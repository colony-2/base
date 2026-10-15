"""Production-image acceptance for the provider-initialized shared-store contract."""
import argparse
import json
import shlex
import statistics
import time
import unittest
from pathlib import Path

import probe
from compatibility import CHECK, INSPECT


class Acceptance(probe.Probe):
    def options(self, **kwargs):
        return [*super().options(**kwargs), '--mount',
                f'type=bind,src={Path(__file__).resolve().parent},dst=/probe,readonly']

    def fresh(self):
        self.tearDown()
        self.containers, self.volumes, self.networks = [], [], []
        super().fresh()

    def run_in(self, script, **kwargs):
        return super().run_in(script.replace('python3 /probe/preflight.py', 'base-store-preflight'), **kwargs)

    def validate(self, keys):
        self.control(INSPECT)
        command = 'python3 -c ' + shlex.quote(CHECK) + ' ' + ' '.join(self.paths[k] for k in keys)
        return self.run_in(command, source=False)

    def test_empty_tools_and_entrypoint(self):
        self.run_in('test -z "$(ls -A /nix)"; base-smoke-test --tools-only; '
                    'test -z "$(ls -A /nix)"; '
                    'bash -lc \'test "$NIX_REMOTE" = local; test "$(command -v nix)" = /usr/bin/nix; node -e "if (!process.version.startsWith(\\\"v24.\\\")) process.exit(1)"\'')
        for executable, args in [('/usr/bin/nix', ['--version']),
                                 ('/usr/local/bin/c2j', ['version'])]:
            result = probe.docker('run', '--rm', *self.options(), '--entrypoint', executable, probe.IMAGE, *args)
            self.assertIn('version' if args == ['version'] else 'Nix', result.stdout)
        self.run_in('test -z "$(ls -A /nix)"; base-store-preflight')
        failed = self.run_in('base-store-preflight --require-initialized', check=False)
        self.assertNotEqual(failed.returncode, 0)
        # Provider initialization, once, before exposing the volume to workers.
        self.run_in('base-store-preflight; nix-store --init; base-store-preflight --require-initialized')
        self.run_in('base-smoke-test')
        # No-volume invocation initializes an independent ephemeral store normally.
        probe.docker('run', '--rm', '--platform', probe.PLATFORM, '--network', 'none',
                     probe.IMAGE, 'sh', '-ec', 'test -z "$(ls -A /nix)"; nix-store --init; base-store-preflight --require-initialized')

    def test_signed_cold_and_offline_warm(self):
        producer = self.run_in('nix-store -r ' + self.paths['hello'])
        self.assertIn('copying path', producer.stderr)
        probe.docker('rm', producer.args)
        result = self.run_in('nix-store -r ' + self.paths['hello'] + '; ' + self.paths['hello'] + '/bin/hello', source=False)
        self.assertIn('Hello, world!', result.stdout)
        self.assertNotIn('copying path', result.stderr)
        self.validate(['hello'])
        sigs = self.run_in('nix path-info --sigs ' + self.paths['hello'], source=False)
        self.assertIn('cache.nixos.org-1:', sigs.stdout)
        # Exact-path modern build/out-link is also the operation used by c2j.
        self.run_in('nix build --out-link /tmp/result ' + self.paths['hello'] + '; /tmp/result/bin/hello', source=False)

    def test_preinitialized_concurrent_installs(self):
        for keys in [('figlet', 'figlet'), ('figlet', 'toilet')]:
            for iteration in range(probe.ROUNDS):
                with self.subTest(keys=keys, iteration=iteration):
                    self.fresh()
                    self.run_in('nix-store --init; base-store-preflight --require-initialized')
                    self.pair([['nix-store', '-r', self.paths[key]] for key in keys])
                    self.validate(sorted(set(keys)))

    def test_unsigned_substitutes_are_rejected(self):
        self.run_in('nix-store --init')
        self.control('''import pathlib, shutil
destination=pathlib.Path('/coord/unsigned')
shutil.copytree('/fixture/binary',destination)
for info in destination.glob('*.narinfo'):
    info.write_text('\\n'.join(line for line in info.read_text().splitlines() if not line.startswith('Sig:'))+'\\n')
''')
        result = self.run_in('nix-store -r ' + self.paths['hello'],
                             source='file:///coord/unsigned?trusted=false', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not signed', result.stderr.lower())
        self.assertNotEqual(self.run_in('nix-store --check-validity ' + self.paths['hello'],
                                        source=False, check=False).returncode, 0)

    def test_c2j_child_process(self):
        self.run_in('nix-store --init')
        # An actual recipe subprocess installs a signed package with ordinary Nix.
        recipe = {
            'id': 'base_shared_nix', 'version': '1.0', 'input_schema': {},
            'sequence': [{'id': 'install', 'op': 'command_execution', 'inputs': {
                'run': 'nix-store --realise ' + self.paths['hello'] + ' >/dev/null && ' + self.paths['hello'] + '/bin/hello'}}],
            'outputs': {'message': '${{ sequence.install.outputs.stdout }}'},
        }
        suite = {'cases': [{'id': 'shared_nix', 'type': 'recipe_case',
                            'mocks': {'ops': [{'match': {'op': 'command_execution'},
                                               'behavior': {'mode': 'passthrough'}}]},
                            'assertions': [{'type': 'output_equals', 'path': 'message',
                                            'value': 'Hello, world!'}]}]}
        self.control('from pathlib import Path; Path("/coord/recipe.yaml").write_text(' + repr(json.dumps(recipe)) + '); '
                     'Path("/coord/recipe.test.yaml").write_text(' + repr(json.dumps(suite)) + ')')
        result = self.run_in('c2j test run --recipe-file /coord/recipe.yaml --file /coord/recipe.test.yaml --artifact-mode inline')
        print('c2j_recipe=' + result.stdout, flush=True)
        probe.docker('rm', result.args)
        self.validate(['hello'])
        self.run_in('c2j run with-lease --help >/dev/null; nix-store --check-validity ' + self.paths['hello'], source=False)

    def test_startup_timings(self):
        reports = {}
        started = self.run_in('python3 -c ' + shlex.quote(
            'import subprocess,time; start=time.monotonic(); '
            'subprocess.run(["nix-store","--init"],check=True); print(time.monotonic()-start)'))
        reports['nix_initialize_seconds'] = float(started.stdout)
        for state in ('empty', 'populated'):
            if state == 'populated':
                self.run_in('nix-store -r ' + ' '.join(self.paths.values()))
                self.run_in('python3 -c ' + shlex.quote('''
from pathlib import Path
import subprocess
root=Path('/tmp/objects'); root.mkdir()
for i in range(1000): (root/str(i)).write_text('startup fixture '+str(i))
subprocess.run(['nix-store','--add',*[str(p) for p in root.iterdir()]],check=True,stdout=subprocess.DEVNULL)
'''))
            samples = []
            for _ in range(5):
                start = time.monotonic()
                self.run_in('true', source=False)
                samples.append(time.monotonic() - start)
            reports[state] = {'docker_create_start_wait_seconds_median': statistics.median(samples),
                              'max': max(samples)}
        # Timing inside a single container separates command cost from Docker.
        result = self.run_in('python3 -c ' + shlex.quote('''
import json, statistics, subprocess, time
commands={'execute': [''' + repr(self.paths['hello'] + '/bin/hello') + '''],
          'lookup': ['nix-store','--check-validity',''' + repr(self.paths['hello']) + ''']}
result={}
for name,argv in commands.items():
    samples=[]
    for _ in range(10):
        start=time.monotonic()
        subprocess.run(argv,check=True,stdout=subprocess.DEVNULL)
        samples.append(time.monotonic()-start)
    result[name]={'median_seconds':statistics.median(samples),'max_seconds':max(samples)}
print(json.dumps(result))
'''), source=False)
        reports['commands'] = json.loads(result.stdout)
        print('timings=' + json.dumps(reports), flush=True)

    def test_distinct_image_revisions(self):
        original = probe.IMAGE
        peer = probe.name() + ':peer'
        self.addCleanup(lambda: probe.docker('image', 'rm', peer, check=False))
        # A second image revision with the exact same certified Nix package.
        probe.docker('build', '--platform', probe.PLATFORM, '--build-arg', 'BASE=' + original,
                     '-t', peer, '-', input='ARG BASE\nFROM ${BASE}\nLABEL io.colony-2.base.test-revision=peer\n', timeout=120)
        self.assertNotEqual(probe.docker('image', 'inspect', original, '--format', '{{.Id}}').stdout,
                            probe.docker('image', 'inspect', peer, '--format', '{{.Id}}').stdout)
        try:
            for producer, consumer in [(original, peer), (peer, original)]:
                self.fresh()
                probe.IMAGE = producer
                done = self.run_in('nix-store -r ' + self.paths['hello'])
                probe.docker('rm', done.args)
                probe.IMAGE = consumer
                self.validate(['hello'])
                # Keep each Nix process running across the barrier in its own image.
                first = self.create(['python3', '/probe/worker.py', '0', 'nix-store', '-r', self.paths['figlet']])
                probe.IMAGE = producer
                second = self.create(['python3', '/probe/worker.py', '1', 'nix-store', '-r', self.paths['toilet']])
                for container in (first, second): probe.docker('start', container)
                self.wait_for('all(Path("/coord/ready-"+str(i)).exists() for i in range(2))')
                self.touch('/coord/go')
                for container in (first, second): self.finish(container)
                self.validate(['hello', 'figlet', 'toilet'])
        finally:
            probe.IMAGE = original


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='colony-base:ubuntu-shared')
    parser.add_argument('--platform', default='linux/arm64')
    parser.add_argument('--rounds', type=int, default=3)
    args = parser.parse_args()
    probe.IMAGE, probe.PLATFORM, probe.ROUNDS = args.image, args.platform, args.rounds
    cases = [
        'test_empty_tools_and_entrypoint', 'test_signed_cold_and_offline_warm',
        'test_preinitialized_concurrent_installs',
        'test_06_concurrent_private_default_profiles',
        'test_07_interrupted_substitution_then_same_pid_retry',
        'test_11_preflight_rejects_bad_ownership_and_modes',
        'test_12_readonly_fails_and_preflight_leaves_store_unmodified',
        'test_13_concurrent_preflight_on_empty_volume',
        'test_14_no_builds_including_prefer_local',
        'test_unsigned_substitutes_are_rejected', 'test_c2j_child_process', 'test_startup_timings',
        'test_distinct_image_revisions',
    ]
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(Acceptance(case) for case in cases))
    raise SystemExit(not result.wasSuccessful())
