import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('wrapper', Path(__file__).resolve().parents[1] / 'scripts/nix-cache-wrapper.py')
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


class FakeRunner:
    cancelled = 0
    last_pid = 42

    def __init__(self):
        self.valid = set()
        self.calls = []
        self.failure = None

    def run(self, argv, env, **kwargs):
        self.calls.append((argv, env, kwargs))
        if 'path-info' in argv:
            return 0, json.dumps({p: {} for p in self.valid}).encode()
        if self.failure:
            raise self.failure
        return 0, b''


class CacheStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.runner = FakeRunner()
        self.cache = w.Cache(str(self.base / 'cache with space#%'), {'NIX_CONFIG':'extra-substituters = https://private.example\n'}, self.runner)
        self.state_patch = patch.object(w, 'STATE_ROOT', self.base / 'state')
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        # Filesystem ownership is covered in root Docker acceptance tests.
        self.owned = patch.object(w, 'owned_directory', lambda path, mode=0o755: path.mkdir(parents=True, exist_ok=True, mode=mode))
        self.owned.start()
        self.addCleanup(self.owned.stop)
        self.lock = patch.object(w, 'lock_file', lambda path: os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600))
        self.lock.start()
        self.addCleanup(self.lock.stop)
        self.cache.state()
        self.addCleanup(self.cache.db.close)
        self.cache.root.mkdir()
        self.cache.ready = True

    def pending(self):
        return {row[0] for row in self.cache.db.execute('SELECT path FROM pending')}

    def test_environment_preserves_private_configuration_and_trust(self):
        env = self.cache.command_env()
        self.assertIn('https://private.example', env['NIX_CONFIG'])
        self.assertIn('file://', env['NIX_CONFIG'])
        self.assertIn('%20', env['NIX_CONFIG'])
        self.assertIn('%23%25', env['NIX_CONFIG'])
        self.assertIn('trusted=false', env['NIX_CONFIG'])
        self.assertIn('priority=1', env['NIX_CONFIG'])
        self.assertIn('narinfo-cache-negative-ttl = 0', env['NIX_CONFIG'])
        self.assertNotIn('require-sigs', env['NIX_CONFIG'])
        self.assertEqual(env['NIX_REMOTE'], 'local')
        nested = w.Cache(str(self.cache.root), w.clean_environment(env), self.runner)
        nested.ready = True
        self.assertEqual(nested.command_env()['NIX_CONFIG'].count('extra-substituters = file:'), 1)
        self.assertEqual(w.clean_environment(env)['NIX_CONFIG'], self.cache.env['NIX_CONFIG'])

    def test_no_pending_means_no_export_or_query(self):
        self.cache.export()
        self.assertEqual(self.runner.calls, [])

    def test_exact_registered_paths_export_with_stdin_and_no_substitution(self):
        self.runner.valid = {'/nix/store/a-tool', '/nix/store/b-tool'}
        self.cache.remember(self.runner.valid)
        self.cache.export()
        self.assertEqual(self.pending(), set())
        args, _env, options = self.runner.calls[-1]
        self.assertEqual(args[0], str(w.REAL_BIN / 'nix'))
        self.assertIn('--stdin', args)
        self.assertEqual(args[2:5], ['--option','substitute','false'])
        self.assertEqual(options['data'], b'/nix/store/a-tool\n/nix/store/b-tool\n')
        self.assertEqual(len(options['pass_fds']), 1)

    def test_failed_export_survives_next_invocation(self):
        self.runner.valid = {'/nix/store/a-tool'}
        self.cache.remember(self.runner.valid)
        self.runner.failure = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.cache.export()
        self.assertEqual(self.pending(), self.runner.valid)
        self.runner.failure = None
        other = w.Cache(str(self.cache.root), {}, self.runner)
        other.db = sqlite3.connect(self.cache.db.execute('PRAGMA database_list').fetchone()[2])
        other.ready = True
        try:
            other.export()
        finally:
            other.db.close()
        self.assertEqual(self.pending(), set())

    def test_removed_paths_are_not_realised(self):
        self.cache.remember({'/nix/store/gone'})
        with contextlib.redirect_stderr(io.StringIO()):
            self.cache.export()
        self.assertEqual(self.pending(), set())
        self.assertEqual(len(self.runner.calls), 1)

    def test_store_query_counts_toward_export_deadline(self):
        self.cache.export_timeout = 0.01
        self.cache.remember({'/nix/store/a'})
        def slow_query(timeout):
            self.assertLessEqual(timeout, self.cache.export_timeout)
            time.sleep(0.02)
            return {'/nix/store/a'}
        with patch.object(self.cache, 'paths', slow_query), self.assertRaises(TimeoutError):
            self.cache.export()
        self.assertEqual(self.pending(), {'/nix/store/a'})
        self.assertEqual(self.runner.calls, [])

    def test_concurrent_bookkeeping_merges_sets(self):
        other = w.Cache(str(self.cache.root), {}, self.runner)
        other.db = sqlite3.connect(self.cache.db.execute('PRAGMA database_list').fetchone()[2])
        try:
            self.cache.remember({'/nix/store/a', '/nix/store/common'})
            other.remember({'/nix/store/b', '/nix/store/common'})
            self.assertEqual(self.pending(), {'/nix/store/a','/nix/store/b','/nix/store/common'})
        finally:
            other.db.close()

    def test_timeout_inputs_are_finite_and_positive(self):
        for value in ['0', '-1', 'nan', 'inf', 'nonsense']:
            with self.subTest(value=value), self.assertRaises(w.CacheError):
                w.duration({'X':value}, 'X', 30)
        self.assertEqual(w.duration({}, 'X', 30), 30)

    def test_relative_cache_path_rejected(self):
        for root in ['relative', '/tmp/cache\nsubstituters = evil']:
            with self.assertRaises(w.CacheError):
                w.Cache(root, {}, self.runner)


class RunnerTests(unittest.TestCase):
    def test_closed_stderr_does_not_fall_back_to_stdout(self):
        output = io.StringIO()
        with patch.object(sys, 'stderr', None), contextlib.redirect_stdout(output):
            w.diagnostic('test', 'must not reach stdout')
        self.assertEqual(output.getvalue(), '')
        broken = io.StringIO()
        broken.close()
        with patch.object(sys, 'stderr', broken):
            w.diagnostic('test', 'must not change the command status')
        with patch.object(os, 'write', side_effect=OSError('unwritable stderr')):
            w.diagnostic('test', 'must not buffer a failed write')

    def test_all_disabled_wrappers_exec_exact_argv_and_environment(self):
        environment = {'PATH':'/usr/local/bin:/bin', 'COLONY_NIX_CACHE_DIR':'', 'TOKEN':'not-for-logging'}
        for command in w.COMMANDS:
            argv = [command, '--argstr', 'value', 'space "quote" $literal\nnewline']
            with self.subTest(command=command), patch.dict(os.environ, environment, clear=True), \
                 patch.object(sys, 'argv', argv), \
                 patch.object(os, 'execve', side_effect=SystemExit) as execute:
                with self.assertRaises(SystemExit):
                    w.main()
                real = str(w.REAL_BIN / command)
                execute.assert_called_once_with(real, [real, *argv[1:]], environment)

    def test_internal_stdout_is_captured_not_leaked(self):
        status, output = w.Runner().run([sys.executable, '-c', 'print("json")'], os.environ)
        self.assertEqual((status, output), (0, b'json\n'))

    def test_export_timeout_kills_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = str(Path(directory) / 'escaped')
            script = ('import subprocess,time,sys; '
                      'subprocess.Popen([sys.executable,"-c",'
                      + repr(f'import time; time.sleep(0.4); open({marker!r},"w").close()') + ']); time.sleep(10)')
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                w.Runner().run([sys.executable, '-c', script], os.environ, timeout=0.1)
            self.assertLess(time.monotonic()-started, 2)
            time.sleep(0.5)
            self.assertFalse(Path(marker).exists())

    def test_nonzero_status_and_argument_boundaries(self):
        value = 'space "quote" $literal\nnewline'
        status, output = w.Runner().run([sys.executable, '-c', 'import sys; print(repr(sys.argv[1])); sys.exit(23)',value], os.environ)
        self.assertEqual(status, 23)
        self.assertEqual(output.decode().strip(), repr(value))

    def test_lock_timeout_keeps_same_inode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'lock'
            # Root ownership enforcement has separate Docker tests.
            with patch.object(w, 'lock_file', lambda p: os.open(p, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)):
                with w.locked(path, 1, w.Runner()):
                    inode = path.stat().st_ino
                    with self.assertRaises(w.LockTimeout), w.locked(path, 0.05, w.Runner()):
                        pass
                with w.locked(path, 1, w.Runner()):
                    self.assertEqual(path.stat().st_ino, inode)


if __name__ == '__main__':
    unittest.main()
