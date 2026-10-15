"""Test real mixed-version local stores without building the production image.

Uses the signed fixture and Docker isolation helpers from probe.py. Store
initialization is serial: cold-start races are covered by the separate probe.
"""
import argparse
import itertools
import json
import unittest
from concurrent.futures import ThreadPoolExecutor

import probe

IMAGES = {
    '2.6.0': 'colony-base:nix-compat-2.6',
    '2.18.1': 'colony-base:nix-compat-2.18',
    '2.34.3': 'colony-base:shared-store-probe',
}
ROUNDS = 3

# Inspect before opening through another Nix version. No verify/repair command
# that could silently repair database inconsistencies is used in these tests.
INSPECT = '''
import json, pathlib, sqlite3
root=pathlib.Path('/nix/var/nix/db')
db=sqlite3.connect('file:'+str(root/'db.sqlite')+'?mode=ro', uri=True)
integrity=db.execute('pragma integrity_check').fetchall()
foreign=db.execute('pragma foreign_key_check').fetchall()
assert integrity==[('ok',)], integrity
assert not foreign, foreign
tables=[r[0] for r in db.execute("select name from sqlite_master where type='table' order by name")]
print(json.dumps(dict(schema=(root/'schema').read_text(),
    ca_schema=(root/'ca-schema').read_text() if (root/'ca-schema').exists() else None,
    migrations=list(db.execute('select migration from SchemaMigrations order by migration')) if 'SchemaMigrations' in tables else [],
    paths=db.execute('select count(*) from ValidPaths').fetchone()[0],
    integrity='ok', tables=tables)))
'''

CHECK = '''
import json, subprocess, sys
paths=sys.argv[1:]
closure=subprocess.check_output(['nix-store','--query','--requisites',*paths],text=True).split()
subprocess.run(['nix-store','--check-validity',*closure],check=True)
for path in closure:
    recorded=subprocess.check_output(['nix-store','--query','--hash',path],text=True).strip()
    actual=subprocess.check_output(['nix-hash','--type','sha256','--base32',path],text=True).strip()
    assert recorded=='sha256:'+actual, (path,recorded,actual)
commands={'hello':['hello'], 'figlet':['figlet','shared'], 'toilet':['toilet','-f','term','shared']}
for path in paths:
    key=next(k for k in commands if '-'+k+'-' in path)
    argv=commands[key]
    subprocess.run([path+'/bin/'+argv[0],*argv[1:]],check=True,stdout=subprocess.DEVNULL)
print(json.dumps(dict(checked_paths=len(closure), executed=len(paths))))
'''


class Compatibility(probe.Probe):
    image = IMAGES['2.34.3']

    def fresh(self):
        # Bound disk usage and avoid accumulating idle helper containers.
        self.tearDown()
        self.containers, self.volumes, self.networks = [], [], []
        super().fresh()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for version, image in IMAGES.items():
            actual = probe.docker('run', '--rm', '--platform', probe.PLATFORM,
                                  '--network', 'none', image, 'nix', '--version').stdout.strip()
            assert actual == 'nix (Nix) ' + version, actual
            identity = probe.docker('image', 'inspect', image, '--format', '{{.Id}}').stdout.strip()
            print(json.dumps({'version': version, 'image': image, 'identity': identity}), flush=True)

    def create(self, argv, **kwargs):
        container = probe.name()
        self.containers.append(container)
        probe.docker('create', '--name', container, *self.options(**kwargs), self.image, *argv)
        return container

    def command(self, version, argv, check=True, source=True):
        self.image = IMAGES[version]
        container = self.create(argv, source=source)
        probe.docker('start', container)
        return self.finish(container, check=check)

    def snapshot(self):
        value = json.loads(self.control(INSPECT).stdout)
        print('database=' + json.dumps(value), flush=True)
        return value

    def check_paths(self, version, keys):
        return self.command(version, ['python3', '-c', CHECK,
                                      *[self.paths[k] for k in keys]], source=False)

    def initialize(self, version, keys=()):
        self.command(version, ['nix-store', '--init'])
        if keys:
            self.command(version, ['nix-store', '--realise', *[self.paths[k] for k in keys]])

    def concurrent(self, versions, keys):
        containers = []
        for i, (version, key) in enumerate(zip(versions, keys)):
            self.image = IMAGES[version]
            containers.append(self.create(['python3', '/probe/worker.py', str(i),
                                            'nix-store', '--realise', self.paths[key]]))
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda c: probe.docker('start', c), containers))
        self.wait_for('all(Path("/coord/ready-"+str(i)).exists() for i in range(2))')
        ready = json.loads(self.control(
            'import json; from pathlib import Path; print(json.dumps([json.loads(p.read_text()) for p in Path("/coord").glob("ready-*")]))').stdout)
        self.assertEqual({r['pid'] for r in ready}, {1})
        self.assertEqual(len({r['namespace'] for r in ready}), 2)
        self.touch('/coord/go')
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda c: self.finish(c, check=False), containers))
        for result in results:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_sequential_versions(self):
        for a, b in itertools.combinations(IMAGES, 2):
            for sequence in [(a, b, a), (b, a, b)]:
                with self.subTest(sequence=sequence):
                    self.fresh()
                    seen = []
                    for version, key in zip(sequence, ['hello', 'figlet', 'toilet']):
                        self.command(version, ['nix-store', '--realise', self.paths[key]])
                        seen.append(key)
                        self.snapshot()
                        self.check_paths(version, seen)
                    for version in (a, b):
                        self.check_paths(version, seen)
                    print('PASS sequential ' + ' -> '.join(sequence), flush=True)

    def test_concurrent_versions(self):
        for versions in itertools.combinations(IMAGES, 2):
            for initializer in versions:
                for keys in [('figlet', 'figlet'), ('figlet', 'toilet')]:
                    for round_number in range(ROUNDS):
                        with self.subTest(versions=versions, initializer=initializer,
                                          keys=keys, round=round_number):
                            self.fresh()
                            self.initialize(initializer, ['hello'])
                            self.concurrent(versions, keys)
                            self.snapshot()
                            for version in versions:
                                self.check_paths(version, ['hello', *sorted(set(keys))])
                            print(f'PASS concurrent {versions} init={initializer} keys={keys} round={round_number+1}', flush=True)

    def test_experimental_schema_downgrade(self):
        # A genuine schema migration, not a manually edited version marker.
        # ca-derivations is deliberately outside the proposed production config.
        self.fresh()
        for version, expected in [('2.6.0', '3'), ('2.18.1', '4')]:
            self.command(version, ['nix-store', '--extra-experimental-features',
                                   'ca-derivations', '--init'])
            self.assertEqual(self.snapshot()['ca_schema'], expected)
        result = self.command('2.6.0', ['nix-store', '--extra-experimental-features',
                                      'ca-derivations', '--init'], check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ca-schema is version 4, but I only support 3', result.stderr)
        print('EXPECTED downgrade rejection: ' + result.stderr.strip(), flush=True)
        # The default feature set can still open the same main-schema-10 store.
        self.initialize('2.6.0', ['hello'])
        self.check_paths('2.18.1', ['hello'])
        self.snapshot()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', default='linux/arm64')
    parser.add_argument('--rounds', type=int, default=3)
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error('--rounds must be positive')
    probe.PLATFORM, ROUNDS = args.platform, args.rounds
    # Select only these cases; do not rerun inherited single-version tests.
    suite = unittest.TestSuite(Compatibility(test) for test in [
        'test_sequential_versions', 'test_concurrent_versions',
        'test_experimental_schema_downgrade'])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
