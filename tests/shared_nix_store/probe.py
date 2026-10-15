"""Focused shared-store feasibility tests; deliberately not the production image.

Run with Python 3.11+, Docker, and the image built from the adjacent Dockerfile.
Failures represent unmet contract requirements, not expected-success assertions.
"""
import argparse
import io
import json
import shlex
import subprocess
import sys
import tarfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor

IMAGE = 'colony-base:shared-store-probe'
PLATFORM = 'linux/arm64'
ROUNDS = 5
NIXPKGS = '39ad350a0602fa0a58a544344e3e9187526ea45c'


def docker(*args, check=True, timeout=90, input=None):
    result = subprocess.run(['docker', *args], text=True, input=input,
                            capture_output=True, timeout=timeout, check=False)
    if check and result.returncode:
        raise AssertionError(f'docker {args[:3]}: {result.returncode}\n{result.stdout}\n{result.stderr}')
    return result


def name():
    return 'shared-nix-probe-' + uuid.uuid4().hex


def new_volume():
    value = name()
    docker('volume', 'create', value)
    return value


class Probe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = new_volume()
        cls.addClassCleanup(lambda: docker('volume', 'rm', cls.fixture, check=False))
        container = name()
        cls.addClassCleanup(lambda: docker('rm', '-f', container, check=False))
        script = f'''
import json, pathlib, subprocess
expr='let p = import (builtins.fetchTarball "https://github.com/NixOS/nixpkgs/archive/{NIXPKGS}.tar.gz") {{}}; in {{ hello = p.hello.outPath; figlet = p.figlet.outPath; toilet = p.toilet.outPath; }}'
paths=json.loads(subprocess.check_output(['nix','eval','--impure','--json','--expr',expr]))
subprocess.run(['nix','copy','--from','https://cache.nixos.org','--to','file:///fixture/binary',*paths.values()],check=True)
pathlib.Path('/fixture/paths.json').write_text(json.dumps(paths))
print(json.dumps(paths))
'''
        result = docker('run', '--rm', '--name', container, '--platform', PLATFORM,
                        '--mount', f'type=volume,src={cls.fixture},dst=/fixture,volume-nocopy',
                        IMAGE, 'python3', '-c', script, timeout=240)
        cls.paths = json.loads(result.stdout)
        trace = docker('run', '--rm', '--platform', PLATFORM, '--network', 'none',
                       IMAGE, 'strace', '-o', '/dev/null', 'true', check=False)
        cls.can_trace = trace.returncode == 0
        daemon_arch = docker('info', '--format', '{{.Architecture}}').stdout.strip()
        aliases = {'aarch64': 'arm64', 'x86_64': 'amd64'}
        cls.emulated = PLATFORM.split('/')[-1] != aliases.get(daemon_arch, daemon_arch)
        print('fixture_paths=' + json.dumps(cls.paths), flush=True)
        print('ptrace_available=' + str(cls.can_trace), flush=True)

    def setUp(self):
        self.containers = []
        self.volumes = []
        self.networks = []
        self.fresh()

    def tearDown(self):
        for container in reversed(self.containers):
            docker('rm', '-f', container, check=False)
        for volume in self.volumes:
            docker('volume', 'rm', volume, check=False)
        for network in self.networks:
            docker('network', 'rm', network, check=False)

    def fresh(self):
        self.store, self.coord = new_volume(), new_volume()
        self.volumes.extend([self.store, self.coord])
        self.helper = self.create(['sleep', '300'])
        docker('start', self.helper)

    def options(self, *, readonly=False, source=True, network='none'):
        return ['--platform', PLATFORM, '--network', network,
                '--mount', f'type=volume,src={self.store},dst=/nix,volume-nocopy' + (',readonly' if readonly else ''),
                '--mount', f'type=volume,src={self.coord},dst=/coord,volume-nocopy',
                '--mount', f'type=volume,src={self.fixture},dst=/fixture,volume-nocopy,readonly',
                '-e', 'NIX_CONFIG=extra-substituters =\nsubstituters = ' +
                ('file:///fixture/binary?trusted=false' if source is True else (source or ''))]

    def create(self, argv, **kwargs):
        container = name()
        self.containers.append(container)
        docker('create', '--name', container, *self.options(**kwargs), IMAGE, *argv)
        return container

    def run_in(self, script, *, check=True, **kwargs):
        container = self.create(['bash', '-euc', script], **kwargs)
        docker('start', container)
        return self.finish(container, check=check)

    def finish(self, container, *, check=True, timeout=90):
        result = docker('wait', container, timeout=timeout)
        status = int(result.stdout.strip())
        logs = docker('logs', container)
        if check and status:
            self.fail(f'container {container}: exit {status}\n{logs.stdout}\n{logs.stderr}')
        return subprocess.CompletedProcess(container, status, logs.stdout, logs.stderr)

    def control(self, code):
        return docker('exec', self.helper, 'python3', '-c', code)

    def wait_for(self, expression, timeout=15):
        code = f'''import os, time
from pathlib import Path
deadline=time.monotonic()+{timeout}
while not ({expression}):
    if time.monotonic()>deadline: raise SystemExit('condition timeout: '+{expression!r})
    time.sleep(0.01)
'''
        self.control(code)

    def touch(self, filename):
        self.control(f'from pathlib import Path; Path({filename!r}).touch()')

    def validate(self, keys):
        paths = [self.paths[key] for key in keys]
        script = 'nix-store --check-validity ' + ' '.join(paths)
        script += '; nix-store --verify --check-contents'
        for key in keys:
            command = {'hello': 'hello', 'figlet': 'figlet shared', 'toilet': 'toilet -f term shared'}[key]
            script += '; ' + self.paths[key] + '/bin/' + command
        return self.run_in(script, source=False)

    def pair(self, commands):
        containers = [self.create(['python3', '/probe/worker.py', str(i), *argv])
                      for i, argv in enumerate(commands)]
        with ThreadPoolExecutor(len(containers)) as pool:
            list(pool.map(lambda c: docker('start', c), containers))
        self.wait_for('all(Path("/coord/ready-"+str(i)).exists() for i in range(' + str(len(commands)) + '))')
        ready = json.loads(self.control(
            'import json; from pathlib import Path; print(json.dumps([json.loads(p.read_text()) for p in Path("/coord").glob("ready-*")]))').stdout)
        self.assertEqual({entry['pid'] for entry in ready}, {1})
        self.assertEqual(len({entry['namespace'] for entry in ready}), len(commands))
        self.touch('/coord/go')
        with ThreadPoolExecutor(len(containers)) as pool:
            results = list(pool.map(lambda c: self.finish(c, check=False), containers))
        for result in results:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return containers

    def test_01_empty_store_and_preflight(self):
        self.run_in('test -z "$(ls -A /nix)"; nix --version; python3 /probe/preflight.py; '
                    'test -z "$(ls -A /nix)"; nix-store --init; test -s /nix/var/nix/db/schema')
        self.run_in('python3 /probe/preflight.py')

    def test_02_signed_warm_reuse(self):
        result = self.run_in('nix-store -r ' + self.paths['hello'])
        self.assertIn('copying path', result.stderr)
        docker('rm', result.args)
        result = self.validate(['hello'])
        self.assertIn('Hello, world!', result.stdout)
        self.assertNotIn('copying path', result.stderr)
        result = self.run_in('nix path-info --sigs ' + self.paths['hello'])
        self.assertIn('cache.nixos.org-1:', result.stdout)

    def test_03_first_use_identical_same_pid(self):
        for iteration in range(ROUNDS):
            with self.subTest(iteration=iteration):
                if iteration:
                    self.fresh()
                self.pair([['nix-store', '-r', self.paths['hello']]] * 2)
                self.validate(['hello'])

    def test_04_first_use_different_same_pid(self):
        for iteration in range(ROUNDS):
            with self.subTest(iteration=iteration):
                if iteration:
                    self.fresh()
                self.pair([['nix-store', '-r', self.paths[key]] for key in ('hello', 'toilet')])
                self.validate(['hello', 'toilet'])

    def test_05_populated_store_concurrent_substitutions(self):
        for iteration in range(ROUNDS):
            with self.subTest(iteration=iteration):
                if iteration:
                    self.fresh()
                self.run_in('nix-store -r ' + self.paths['hello'])
                self.pair([['nix-store', '-r', self.paths[key]] for key in ('figlet', 'toilet')])
                self.validate(['hello', 'figlet', 'toilet'])

    def test_06_concurrent_private_default_profiles(self):
        self.run_in('nix-store --init')
        containers = self.pair([['nix', '--option', 'substitute', 'true', 'profile', 'install', self.paths[key]]
                                for key in ('hello', 'figlet')])
        targets = []
        for container in containers:
            raw = subprocess.check_output(['docker', 'cp', container + ':/root/.local/state/nix/profiles', '-'], timeout=30)
            with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
                targets.append(next(m.linkname for m in archive.getmembers() if m.name.endswith('/profile-1-link')))
        self.assertNotEqual(*targets)
        empty = self.run_in('nix profile list --json')
        self.assertEqual(json.loads(empty.stdout)['elements'], {})
        self.validate(['hello', 'figlet'])

    def test_07_interrupted_substitution_then_same_pid_retry(self):
        for signal in ('TERM', 'KILL'):
            with self.subTest(signal=signal):
                if signal == 'KILL':
                    self.fresh()
                self.run_in('nix-store -r ' + self.paths['hello'])
                path = self.paths['toilet']
                info = path.split('/')[3].split('-')[0] + '.narinfo'
                self.control(f'from pathlib import Path; s=Path("/fixture/binary/{info}").read_text(); '
                             'Path("/coord/pause-url").write_text(next(line[5:] for line in s.splitlines() if line.startswith("URL: ")))')
                network = name()
                self.networks.append(network)
                docker('network', 'create', '--internal', network)
                server = self.create(['python3', '/probe/server.py'], network=network)
                docker('start', server)
                source = f'http://{server}:8000?trusted=false'
                first = self.create(['python3', '/probe/worker.py', 'first', 'nix-store', '-r', path], source=source, network=network)
                docker('start', first)
                self.touch('/coord/go')
                self.wait_for('Path("/coord/paused").exists()', timeout=25)
                second = self.create(['python3', '/probe/worker.py', 'second', 'nix-store', '-r', path], source=source, network=network)
                docker('start', second)
                self.wait_for('Path("/coord/ready-second").exists()')
                # The paused target is not yet valid; a previous output still is.
                invalid = self.run_in('nix-store --check-validity ' + path, check=False, source=False)
                self.assertNotEqual(invalid.returncode, 0)
                self.validate(['hello'])
                docker('kill', '--signal', signal, first)
                self.finish(first, check=False, timeout=15)
                self.touch('/coord/release')
                self.finish(second)
                self.validate(['hello', 'toilet'])

    def pause_initializer(self):
        if not self.can_trace:
            self.skipTest('ptrace is unavailable under this architecture/emulator')
        # Delay only the real schema open syscall after the kernel creates the
        # file. No return values, bytes, or Nix code are changed by this probe.
        writer = self.create(['strace', '-f', '-P', '/nix/var/nix/db/schema',
                              '-e', 'trace=openat', '-e', 'inject=openat:delay_exit=30s',
                              'nix-store', '--init'])
        docker('start', writer)
        self.wait_for('Path("/nix/var/nix/db/schema").exists()')
        size = self.control('from pathlib import Path; print(Path("/nix/var/nix/db/schema").stat().st_size)').stdout.strip()
        if size != '0' and self.emulated:
            self.skipTest('syscall delay injection ineffective under this emulator; requires native testing')
        self.assertEqual(size, '0', 'did not pause in the intended syscall window')
        return writer

    def test_08_concurrent_first_open_during_schema_publication(self):
        writer = self.pause_initializer()
        result = self.run_in('nix-store --init', check=False)
        docker('kill', writer)
        self.finish(writer, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_09_killed_initializer_recovers_without_manual_repair(self):
        writer = self.pause_initializer()
        docker('kill', writer)
        self.finish(writer, check=False)
        result = self.run_in('nix-store --init', check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_10_stock_nix_ownership_behavior(self):
        self.control('import os; os.chown("/nix",123,123)')
        result = self.run_in('nix-store --init', check=False)
        print(f'stock_nix_misowned_root_exit={result.returncode}', flush=True)
        self.assertNotEqual(result.returncode, 0, 'stock Nix accepted /nix owned by 123:123')

    def test_11_preflight_rejects_bad_ownership_and_modes(self):
        mutations = [
            'os.chown("/nix",123,123)',
            'os.chmod("/nix",0o777)',
            'os.chmod("/nix",0o555)',
            'os.makedirs("/nix/store"); os.chown("/nix/store",123,123)',
            'os.makedirs("/nix/var/nix/db"); os.chown("/nix/var/nix/db",123,123)',
            'os.symlink("/tmp", "/nix/store")',
        ]
        for i, mutation in enumerate(mutations):
            with self.subTest(mutation=mutation):
                if i:
                    self.fresh()
                self.control('import os; ' + mutation)
                result = self.run_in('python3 /probe/preflight.py', check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('rejected attachment', result.stderr)
                self.control('from pathlib import Path; assert not list(Path("/nix").glob(".colony-preflight-*"))')

    def test_12_readonly_fails_and_preflight_leaves_store_unmodified(self):
        self.run_in('nix-store --init')
        result = self.run_in('python3 /probe/preflight.py', readonly=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Read-only file system', result.stderr)
        result = self.run_in('nix-store -r ' + self.paths['hello'], readonly=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        result = self.run_in('nix-store --check-validity ' + self.paths['hello'], source=False, check=False)
        self.assertNotEqual(result.returncode, 0)

    def test_13_concurrent_preflight_on_empty_volume(self):
        self.pair([['python3', '/probe/preflight.py']] * 4)
        self.run_in('test -z "$(ls -A /nix)"')

    def test_14_no_builds_including_prefer_local(self):
        for prefer in ('false', 'true'):
            with self.subTest(prefer=prefer):
                expression = ('derivation { name="shared-probe-missing"; system=builtins.currentSystem; '
                              'builder="/bin/sh"; args=["-c" "touch /tmp/forbidden; echo bad > $out"]; '
                              f'preferLocalBuild={prefer}; }}')
                result = self.run_in('set +e; nix-build --no-out-link --expr ' + shlex.quote(expression) +
                                     '; status=$?; set -e; if test -e /tmp/forbidden; then exit 99; fi; exit "$status"',
                                     source=False, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotEqual(result.returncode, 99, 'forbidden builder ran')
                if prefer == 'true':
                    self.assertIn('local builds are disabled', result.stderr)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', default=IMAGE)
    parser.add_argument('--platform', default=PLATFORM)
    parser.add_argument('--rounds', type=int, default=ROUNDS)
    args, remaining = parser.parse_known_args()
    IMAGE, PLATFORM, ROUNDS = args.image, args.platform, args.rounds
    unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)
