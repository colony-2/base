#!/usr/bin/env python3
"""Build and validate a candidate on a single, native Ubuntu Docker worker."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
STAGES = ('preflight', 'resolution', 'build', 'acceptance', 'cleanup')
DEADLINES = dict(preflight=360, resolution=720, build=3900, acceptance=1020, cleanup=120)
LOG_LIMIT = 128 * 1024
ARCHES = {'x86_64': 'amd64', 'aarch64': 'arm64', 'amd64': 'amd64', 'arm64': 'arm64'}
REQUIRED_CASES = {'test_empty_tools_and_entrypoint', 'test_signed_cold_and_offline_warm',
                  'test_c2j_child_process'}


class VerificationError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def acceptance_ok(result):
    passed = result['passed']
    skipped = result['skipped']
    return (isinstance(passed, list) and isinstance(skipped, list)
            and len(passed) + len(skipped) == result['tests_run']
            and all(isinstance(entry['case'], str) and isinstance(entry['reason'], str)
                    for entry in skipped)
            and REQUIRED_CASES <= {case.rsplit('.', 1)[-1] for case in passed})


class ImageVerification:
    def __init__(self, root, outbox, candidate, base):
        self.root = root
        self.outbox = outbox
        self.prefix = 'base-verify-' + uuid.uuid4().hex + '-'
        self.tag = self.prefix + 'candidate:local'
        self.record = dict(behavior='image', candidate=candidate, base=base,
                           tag=self.tag, status='failed', stages={})
        self.cwd = root
        self.env = {key: value for key, value in os.environ.items() if key not in (
            'GH_TOKEN', 'GITHUB_TOKEN', 'GITHUB_OUTPUT', 'BUILDX_BUILDER', 'BUILDKIT_HOST')}
        self.env.update(BASE_VERIFY_PREFIX=self.prefix, PYTHONDONTWRITEBYTECODE='1')
        self.docker_access = False

    @contextmanager
    def stage(self, name):
        self.step = self.record['stages'][name] = dict(status='failed')
        self.deadline = time.monotonic() + DEADLINES[name]
        with tempfile.TemporaryFile() as self.output:
            try:
                yield
            except (VerificationError, OSError, subprocess.SubprocessError, ValueError,
                    KeyError, TypeError, AttributeError) as error:
                self.step['error'] = str(error)
                self.output.write((str(error) + '\n').encode(errors='replace'))
            else:
                self.step['status'] = 'passed'
            size = self.output.seek(0, os.SEEK_END)
            self.output.seek(max(0, size - LOG_LIMIT))
            raw = self.output.read()
        if size > LOG_LIMIT:
            raw = b'[earlier output truncated]\n' + raw
        self.save_log(name, raw or b'Completed without command output.\n')

    def save_log(self, name, raw):
        (self.outbox / f'image-{name}.log').write_bytes(raw)
        self.record['stages'][name]['log_sha256'] = hashlib.sha256(raw).hexdigest()

    def command(self, *argv, timeout=60, capture=False):
        timeout = min(timeout, max(0, self.deadline - time.monotonic()))
        if not timeout:
            self.step['deadline_seconds'] = timeout
            raise subprocess.TimeoutExpired(argv, timeout)
        self.output.write(('$ ' + ' '.join(argv) + '\n').encode())
        self.output.flush()
        with subprocess.Popen(argv, cwd=self.cwd, env=self.env, start_new_session=True,
                              stdout=subprocess.PIPE if capture else self.output,
                              stderr=self.output) as process:
            try:
                stdout, _ = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
                self.step['deadline_seconds'] = timeout
                raise
        self.output.seek(0, os.SEEK_END)
        if stdout:
            self.output.write(stdout)
        if process.returncode:
            self.step['returncode'] = process.returncode
            raise subprocess.CalledProcessError(process.returncode, argv)
        self.step['returncode'] = 0
        return stdout.decode().strip() if capture else None

    def preflight(self, scratch):
        release = platform.freedesktop_os_release()
        host_arch = ARCHES.get(platform.machine())
        require(release.get('ID') == 'ubuntu' and host_arch, 'Expected a native Ubuntu amd64 or arm64 worker')
        self.record.update(worker_release=release.get('PRETTY_NAME'), platform='linux/' + host_arch,
                           machine=platform.machine())
        candidate = self.record['candidate']
        require(re.fullmatch(r'[0-9a-f]{40}', candidate or ''), 'Expected the exact candidate Git SHA')
        require(self.command('git', 'rev-parse', 'HEAD', capture=True) == candidate,
                'Task worktree does not match the verification candidate')
        checkout = scratch / 'candidate'
        self.command('git', 'clone', '--shared', '--no-checkout', '--', str(self.root), str(checkout))
        self.cwd = checkout
        self.command('git', 'checkout', '--detach', candidate)
        require(self.command('git', 'rev-parse', 'HEAD', capture=True) == candidate,
                'Clean checkout does not match the verification candidate')
        daemon = self.command('docker', 'info', '--format', '{{.OSType}} {{.Architecture}} {{.ServerVersion}}',
                              capture=True).split()
        require(len(daemon) == 3 and daemon[0] == 'linux' and ARCHES.get(daemon[1]) == host_arch,
                'Docker daemon does not match the native worker platform')
        self.docker_access = True
        self.record['docker_server'] = daemon[2]
        self.record['docker_client'] = self.command('docker', 'version', '--format', '{{.Client.Version}}', capture=True)
        self.record['buildx'] = self.command('docker', 'buildx', 'version', capture=True)
        marker = checkout / '.verify-mount'
        marker.write_text(self.prefix)
        self.command('docker', 'run', '--rm', '--name', self.prefix + 'preflight',
                     '--platform', self.record['platform'], '--mount',
                     f'type=bind,src={checkout},dst=/candidate,readonly', 'ubuntu:26.04',
                     'sh', '-ec', 'test "$(cat /candidate/.verify-mount)" = "$1"; '
                     'if touch /candidate/.verify-write 2>/dev/null; then exit 1; fi', 'sh', self.prefix,
                     timeout=180)
        marker.unlink()

    def resolve(self):
        self.command(sys.executable, '-B', 'scripts/resolve-versions.py', '--verify-arch', self.arch, timeout=720)
        inputs = json.loads((self.cwd / '.build/versions.json').read_text())
        fingerprint = inputs['fingerprint']
        require(set(inputs['apt']) == {self.arch} and re.fullmatch(r'[0-9a-f]{64}', fingerprint),
                'Resolved inputs do not match the native verification platform')
        self.record['fingerprint'] = fingerprint

    @property
    def arch(self):
        return self.record['platform'].split('/')[1]

    def inspect_image(self):
        raw = self.command('docker', 'image', 'inspect', self.tag, capture=True)
        image, = json.loads(raw)
        return dict(id=image['Id'], platform=f"{image['Os']}/{image['Architecture']}",
                    revision=image['Config']['Labels']['org.opencontainers.image.revision'])

    def build(self):
        self.command('bash', 'scripts/build-image.sh', self.arch, 'verify-load', self.tag,
                     self.record['candidate'], timeout=DEADLINES['build'])
        image = self.record['image'] = self.inspect_image()
        require(re.fullmatch(r'sha256:[0-9a-f]{64}', image['id'])
                and image['platform'] == self.record['platform']
                and image['revision'] == self.record['candidate'], 'Loaded image has the wrong identity')

    def accept(self):
        image = self.record['image']
        require(self.inspect_image() == image, 'Local image identity changed before acceptance')
        receipt = self.cwd / '.build/acceptance.json'
        self.env['BASE_VERIFY_ACCEPTANCE_REPORT'] = str(receipt)
        try:
            self.command(sys.executable, '-B', 'tests/shared_nix_store/acceptance.py',
                         '--image', self.tag, '--platform', self.record['platform'], timeout=900)
        finally:
            self.record['acceptance_returncode'] = self.step.get('returncode')
            if receipt.is_file():
                self.record['acceptance'] = json.loads(receipt.read_text())
        require(self.inspect_image() == image, 'Local image identity changed during acceptance')
        require(acceptance_ok(self.record.get('acceptance', {})), 'Acceptance did not pass the required cases')

    def cleanup(self):
        failures = []
        for kind, list_options, template, remove_options in (
            ('container', ('-a',), '{{.Names}}', ('-f',)),
            ('volume', (), '{{.Name}}', ()),
            ('network', (), '{{.Name}}', ()),
            ('image', (), '{{.Repository}}:{{.Tag}}', ()),
        ):
            try:
                names = self.command('docker', kind, 'ls', *list_options, '--format', template,
                                     capture=True, timeout=20)
                owned = [name for name in names.splitlines() if name.startswith(self.prefix)]
                if owned:
                    self.command('docker', kind, 'rm', *remove_options, *owned, timeout=40)
            except (OSError, subprocess.SubprocessError) as error:
                failures.append(str(error))
        require(not failures, '; '.join(failures))

    def run(self):
        with tempfile.TemporaryDirectory(prefix='.base-verify-', dir=self.root) as temporary:
            operations = [('preflight', lambda: self.preflight(Path(temporary))),
                          ('resolution', self.resolve), ('build', self.build), ('acceptance', self.accept)]
            try:
                for name, operation in operations:
                    with self.stage(name):
                        operation()
                    if self.record['stages'][name]['status'] != 'passed':
                        break
            finally:
                if self.docker_access:
                    with self.stage('cleanup'):
                        self.cleanup()
        for name in STAGES:
            if name not in self.record['stages']:
                reason = 'Prerequisite stage did not pass.'
                self.record['stages'][name] = dict(status='not run', reason=reason)
                self.save_log(name, (reason + '\n').encode())
        if all(stage['status'] == 'passed' for stage in self.record['stages'].values()):
            self.record['status'] = 'passed'
        (self.outbox / 'image.json').write_text(json.dumps(self.record, indent=2) + '\n')


if __name__ == '__main__':
    ImageVerification(ROOT, Path(os.environ['OUTBOX']), os.environ.get('CANDIDATE'),
                      os.environ.get('BASE')).run()
