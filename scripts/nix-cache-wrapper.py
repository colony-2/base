#!/usr/bin/env python3
"""Best-effort, post-command binary caching. Installed with an absolute shebang."""
import contextlib
import fcntl
import hashlib
import json
import math
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

REAL_BIN = Path('/usr/local/libexec/nix-real')
STATE_ROOT = Path('/var/lib/colony/nix-cache')
COMMANDS = {
    'nix', 'nix-env', 'nix-store', 'nix-build', 'nix-shell',
    'nix-channel', 'nix-copy-closure', 'nix-instantiate', 'nix-prefetch-url',
}
QUERY_TIMEOUT = 10
CANCEL_GRACE = 2
CONFIG_MARKER = '_COLONY_NIX_CACHE_ADDED_CONFIG'


class CacheError(Exception):
    pass


class LockTimeout(CacheError):
    pass


def diagnostic(kind, detail):
    # Never include argv, NIX_CONFIG, underlying errors, or credentials.
    # Python sets sys.stderr to None for a closed fd 2; print(file=None) would
    # fall back to stdout and corrupt JSON/store-path output. Write directly
    # to avoid a failed buffered write changing Python's exit status to 120.
    if sys.stderr is not None:
        with contextlib.suppress(OSError, ValueError):
            os.write(sys.stderr.fileno(), f'colony-nix-cache: {kind}: {detail}\n'.encode())


def clean_environment(source):
    """Remove only a parent wrapper's additions before configuring this call."""
    env = dict(source)
    addition = env.pop(CONFIG_MARKER, '')
    config = env.get('NIX_CONFIG', '')
    if addition and addition in config:
        env['NIX_CONFIG'] = ''.join(config.rsplit(addition, 1))
    return env


def duration(env, name, default):
    try:
        value = float(env.get(name, str(default)))
        if not math.isfinite(value) or value <= 0:
            raise ValueError
        return value
    except ValueError:
        raise CacheError(f'{name} must be a positive finite number') from None


def owned_directory(path, mode=0o755):
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise CacheError('cache directories must be root-owned and not group/world writable')


def lock_file(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_nlink != 1:
        os.close(fd)
        raise CacheError('lock file must be a root-owned regular file with one link')
    return fd


@contextlib.contextmanager
def locked(path, timeout, runner):
    fd = lock_file(path)
    started = time.monotonic()
    try:
        while True:
            if runner.cancelled:
                raise CacheError('canceled')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() - started >= timeout:
                    raise LockTimeout('export lock wait exceeded its deadline') from None
                time.sleep(min(0.05, timeout))
        yield fd, time.monotonic() - started
    finally:
        # Do not unlink the shared inode. The exporter inherits this descriptor,
        # so even SIGKILL of the wrapper cannot expose a still-running writer.
        os.close(fd)


class Runner:
    """Own a child process group, with terminal handoff for the user's command."""
    def __init__(self):
        self.child = None
        self.last_pid = None
        self.cancelled = 0
        self.cancel_at = None

    def on_signal(self, signum, _frame):
        if not self.cancelled:
            self.cancelled = signum
            self.cancel_at = time.monotonic()
        if self.child is not None:
            self.send(signum)

    def send(self, signum):
        try:
            os.killpg(self.child.pid, signum)
        except ProcessLookupError:
            pass

    @staticmethod
    def foreground(fd, group):
        previous = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
        try:
            os.tcsetpgrp(fd, group)
        finally:
            signal.signal(signal.SIGTTOU, previous)

    def run(self, argv, env, *, user=False, timeout=None, data=None, pass_fds=(), quiet=False):
        tty = None
        foreground = None
        if user:
            try:
                tty = os.open('/dev/tty', os.O_RDWR | os.O_CLOEXEC)
                foreground = os.tcgetpgrp(tty)
                if foreground != os.getpgrp():
                    os.close(tty)
                    tty = None
            except OSError:
                if tty is not None:
                    os.close(tty)
                tty = None
        child = subprocess.Popen(
            argv, env=env, process_group=0, pass_fds=pass_fds,
            stdin=None if user else (subprocess.PIPE if data is not None else subprocess.DEVNULL),
            stdout=None if user else subprocess.PIPE,
            stderr=subprocess.DEVNULL if quiet else None,
        )
        self.child = child
        self.last_pid = child.pid
        started = time.monotonic()
        timed_out = False
        try:
            if tty is not None:
                self.foreground(tty, child.pid)
            if user:
                while True:
                    pid, status = os.waitpid(child.pid, os.WNOHANG | os.WUNTRACED)
                    if pid:
                        if os.WIFSTOPPED(status):
                            if tty is not None:
                                self.foreground(tty, foreground)
                            os.kill(os.getpid(), signal.SIGSTOP)
                            if tty is not None:
                                self.foreground(tty, child.pid)
                            self.send(signal.SIGCONT)
                        else:
                            child.returncode = os.waitstatus_to_exitcode(status)
                            return child.returncode, b''
                    if self.cancelled and time.monotonic() - self.cancel_at >= CANCEL_GRACE:
                        self.send(signal.SIGKILL)
                    time.sleep(0.02)
            sent = False
            while True:
                if self.cancelled or (timeout is not None and time.monotonic() - started >= timeout):
                    timed_out = not self.cancelled
                    self.send(signal.SIGKILL)
                try:
                    out, _ = child.communicate(input=data if not sent else None, timeout=0.05)
                    if timed_out:
                        raise TimeoutError
                    return child.returncode, out
                except subprocess.TimeoutExpired:
                    sent = True
        finally:
            if child.returncode is None:
                self.send(signal.SIGKILL)
                child.wait()
            # A canceled command may have left descendants behind.
            if self.cancelled or timed_out:
                self.send(signal.SIGKILL)
            self.child = None
            if tty is not None:
                try:
                    self.foreground(tty, foreground)
                finally:
                    os.close(tty)


class Cache:
    def __init__(self, root, env, runner):
        if not Path(root).is_absolute() or any(ord(c) < 32 for c in root):
            raise CacheError('COLONY_NIX_CACHE_DIR must be an absolute path without control characters')
        self.root = Path(os.path.normpath(root))
        self.binary = self.root / 'binary'
        self.uri = self.binary.as_uri() + '?priority=1&trusted=false'
        self.env = dict(env, NIX_REMOTE='local')
        self.runner = runner
        self.lock_timeout = duration(env, 'COLONY_NIX_CACHE_LOCK_TIMEOUT_SECONDS', 30)
        self.export_timeout = duration(env, 'COLONY_NIX_CACHE_EXPORT_TIMEOUT_SECONDS', 120)
        self.debug = env.get('COLONY_NIX_CACHE_DEBUG') == '1'
        self.db = None
        self.ready = False

    def state(self):
        directory = STATE_ROOT / hashlib.sha256(os.fsencode(self.root)).hexdigest()
        owned_directory(STATE_ROOT, 0o700)
        owned_directory(directory, 0o700)
        database = directory / 'pending.sqlite3'
        if database.exists() or database.is_symlink():
            info = database.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise CacheError('pending database must be a root-owned regular file')
        self.db = sqlite3.connect(database, timeout=3)
        self.db.execute('CREATE TABLE IF NOT EXISTS pending (path TEXT PRIMARY KEY)')
        self.db.commit()

    def paths(self, timeout=QUERY_TIMEOUT):
        status, output = self.runner.run(
            [str(REAL_BIN / 'nix'), 'path-info', '--all', '--json', '--json-format', '1'], self.env,
            timeout=timeout, quiet=True,
        )
        if status:
            raise CacheError('could not query registered local store paths')
        return set(json.loads(output))

    def prepare(self):
        owned_directory(self.root)
        owned_directory(self.binary)
        # An existing cache is read without taking the writer lock. Nix handles
        # metadata/hash validation; a later read error is not retried by us.
        info = self.binary / 'nix-cache-info'
        if not info.exists():
            with locked(self.root / 'export.lock', self.lock_timeout, self.runner) as (fd, wait):
                started = time.monotonic()
                if not info.exists():
                    status, _ = self.runner.run(
                        [str(REAL_BIN / 'nix'), 'copy', '--to', self.uri, '--stdin'],
                        self.env, data=b'', timeout=self.export_timeout, pass_fds=(fd,), quiet=True,
                    )
                    if status:
                        raise CacheError('could not initialize the file binary cache')
                if self.debug:
                    diagnostic('initialization', f'lock_wait_seconds={wait:.3f} initialization_seconds={time.monotonic() - started:.3f}')
        metadata = info.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise CacheError('cache metadata must be a root-owned regular file')
        # Opening the file diagnoses unreadable mounts before the user command.
        with info.open('rb') as stream:
            if b'StoreDir: /nix/store' not in stream.read(4096).splitlines():
                raise CacheError('file cache has an incompatible or invalid StoreDir')
        self.ready = True

    def command_env(self):
        env = self.env.copy()
        if self.ready:
            addition = f'\nextra-substituters = {self.uri}\nnarinfo-cache-negative-ttl = 0\n'
            env['NIX_CONFIG'] = env.get('NIX_CONFIG', '') + addition
            env[CONFIG_MARKER] = addition
        return env

    def remember(self, paths):
        with self.db:
            self.db.executemany('INSERT OR IGNORE INTO pending VALUES (?)', ((p,) for p in paths))

    def export(self):
        pending = {row[0] for row in self.db.execute('SELECT path FROM pending')}
        if not pending or not self.ready or self.runner.cancelled:
            return
        with locked(self.root / 'export.lock', self.lock_timeout, self.runner) as (fd, wait):
            started = time.monotonic()
            # GC may have removed outputs while we waited. Never re-realise them.
            valid = self.paths(timeout=min(QUERY_TIMEOUT, self.export_timeout))
            missing = pending - valid
            if missing:
                diagnostic('export failure', f'{len(missing)} pending paths were removed by local GC')
                with self.db:
                    self.db.executemany('DELETE FROM pending WHERE path = ?', ((p,) for p in missing))
            pending &= valid
            if not pending:
                return
            remaining = self.export_timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError
            try:
                status, _ = self.runner.run(
                    [str(REAL_BIN / 'nix'), 'copy', '--option', 'substitute', 'false',
                     '--to', self.uri, '--stdin'], self.env,
                    data=('\n'.join(sorted(pending)) + '\n').encode(),
                    timeout=remaining, pass_fds=(fd,),
                )
            finally:
                if self.debug:
                    diagnostic('timing', f'lock_wait_seconds={wait:.3f} export_seconds={time.monotonic() - started:.3f} paths={len(pending)} export_pid={self.runner.last_pid}')
            if status:
                raise CacheError('nix copy failed; pending paths retained')
            if not self.runner.cancelled:
                with self.db:
                    self.db.executemany('DELETE FROM pending WHERE path = ?', ((p,) for p in pending))


def main():
    command = Path(sys.argv[0]).name
    if command not in COMMANDS:
        diagnostic('configuration failure', 'unsupported wrapper command')
        return 126
    argv = [str(REAL_BIN / command), *sys.argv[1:]]
    env = clean_environment(os.environ)
    root = env.get('COLONY_NIX_CACHE_DIR', '')
    if not root:
        if env.get('COLONY_NIX_CACHE_DEBUG') == '1':
            diagnostic('cache-disabled', 'COLONY_NIX_CACHE_DIR is unset or empty')
        os.execve(argv[0], argv, env)
    if os.geteuid() != 0:
        diagnostic('initialization failure', 'automatic caching supports only root; running without it')
        os.execve(argv[0], argv, env)
    runner = Runner()
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
        signal.signal(signum, runner.on_signal)
    cache = None
    before = None
    command_env = dict(env, NIX_REMOTE='local')
    try:
        cache = Cache(root, env, runner)
        cache.state()
        before = cache.paths()
        cache.prepare()
        command_env = cache.command_env()
    except LockTimeout:
        diagnostic('lock timeout', 'initialization lock unavailable; running without shared substitution')
    except (CacheError, OSError, sqlite3.Error, TimeoutError, ValueError):
        diagnostic('initialization failure', 'cache unavailable or configuration invalid; running without shared substitution')
    status = 128 + runner.cancelled if runner.cancelled else 1
    if not runner.cancelled:
        status, _ = runner.run(argv, command_env, user=True)
    if cache is not None and before is not None:
        try:
            if runner.cancelled:
                # Best-effort local bookkeeping only on termination: no export
                # or lock wait. Hard kill can still lose unobserved additions.
                cache.runner = Runner()
                cache.remember(cache.paths(timeout=1) - before)
            else:
                cache.remember(cache.paths() - before)
                cache.export()
        except LockTimeout:
            diagnostic('lock timeout', 'pending paths retained for a later command')
        except TimeoutError:
            diagnostic('export timeout', 'pending paths retained for a later command')
        except (CacheError, OSError, sqlite3.Error, ValueError):
            diagnostic('export failure', 'pending paths retained where possible; user status unchanged')
    if cache is not None and cache.db is not None:
        cache.db.close()
    if runner.cancelled or status < 0:
        signum = runner.cancelled or -status
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
        return 128 + signum
    return status


if __name__ == '__main__':
    sys.exit(main())
