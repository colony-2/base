"""Bounded Docker-volume acceptance tests; no host Nix or shared live store.

python3 tests/acceptance_nix_cache.py --image colony-base:cache --platform linux/arm64
"""
import argparse
import json
import re
import shlex
import subprocess
import sys
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor

IMAGE = 'colony-base:cache'
PLATFORM = 'linux/arm64'
ROOT = '/var/cache/colony/nix'


def docker(*args, check=True, timeout=120, input=None):
    result = subprocess.run(['docker', *args], input=input, text=True, capture_output=True, timeout=timeout, check=False)
    if check and result.returncode:
        raise AssertionError(f'Docker {args[:2]} failed ({result.returncode})\n{result.stdout}\n{result.stderr}')
    return result


def volume():
    name = 'colony-cache-test-' + uuid.uuid4().hex
    docker('volume', 'create', name)
    return name


class Acceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = volume()
        fixture_container = 'colony-cache-fixture-' + uuid.uuid4().hex
        try:
            # Fetch real upstream-signed outputs once. All test consumers below
            # have networking disabled, with this read-only upstream fixture.
            script = '''
import hashlib, json, pathlib, subprocess
rev=json.load(open('/usr/local/share/base/versions.json'))['build_args']['NIXPKGS_REV']
expr='let p = import (builtins.fetchTarball "https://github.com/NixOS/nixpkgs/archive/'+rev+'.tar.gz") {}; in { hello = p.hello.outPath; figlet = p.figlet.outPath; toilet = p.toilet.outPath; }'
paths=json.loads(subprocess.check_output(['nix','eval','--impure','--json','--expr',expr]))
subprocess.run(['nix','copy','--from','https://cache.nixos.org','--to','file:///fixture/binary',*paths.values()],check=True)
subprocess.run(['nix','copy','--from','https://cache.nixos.org',paths['hello']],check=True)
with open('/fixture/hello.export','wb') as stream:
    subprocess.run(['nix-store','--export',paths['hello']],stdout=stream,check=True)
content=b'prebuilt legacy fixture\\n'
pathlib.Path('/tmp/cached-fixture').write_bytes(content)
pathlib.Path('/fixture/cached-fixture').write_bytes(content)
paths['fixed']=subprocess.check_output(['nix-store','--add-fixed','sha256','/tmp/cached-fixture'],text=True).strip()
paths['hash']=hashlib.sha256(content).hexdigest()
subprocess.run(['nix','copy','--to','file:///fixture/binary',paths['fixed']],check=True)
system=subprocess.check_output(['nix','eval','--impure','--raw','--expr','builtins.currentSystem'],text=True)
pathlib.Path('/fixture/flake').mkdir()
program=paths['hello']+'/bin/hello'
flake='{ outputs = {self}: { apps.'+system+'.hello = { type = "app"; program = builtins.appendContext "'+program+'" { "'+paths['hello']+'".path = true; }; }; }; }'
pathlib.Path('/fixture/flake/flake.nix').write_text(flake)
print(json.dumps(paths))
'''
            output = docker('run', '--rm', '--name', fixture_container, '--platform', PLATFORM, '--mount',
                            f'type=volume,src={cls.fixture},dst=/fixture,volume-nocopy',
                            IMAGE, 'python3', '-c', script, timeout=240)
            cls.paths = json.loads(output.stdout)
        except BaseException:
            docker('rm', '-f', fixture_container, check=False)
            docker('volume', 'rm', cls.fixture, check=False)
            raise

    @classmethod
    def tearDownClass(cls):
        docker('volume', 'rm', cls.fixture)

    def setUp(self):
        self.cache = volume()
        self.containers = []

    def tearDown(self):
        for container in self.containers:
            docker('rm', '-f', container, check=False)
        docker('volume', 'rm', self.cache)

    def options(self, *, source=True, enabled=True, env=None, root=ROOT, readonly=False):
        options = ['--platform', PLATFORM, '--network', 'none', '--mount',
                   f'type=volume,src={self.cache},dst={root},volume-nocopy' + (',readonly' if readonly else ''),
                   '--mount', f'type=volume,src={self.fixture},dst=/fixture,volume-nocopy,readonly']
        environment = {
            'NIX_CONFIG': 'substituters = file:///fixture/binary' if source else 'substituters =',
            'COLONY_NIX_CACHE_DIR': root if enabled else '',
            'COLONY_NIX_CACHE_DEBUG': '1',
            'COLONY_NIX_CACHE_LOCK_TIMEOUT_SECONDS': '30',
            'COLONY_NIX_CACHE_EXPORT_TIMEOUT_SECONDS': '30',
        }
        environment.update(env or {})
        for key, value in environment.items():
            options.extend(['-e', f'{key}={value}'])
        return options

    def run_in(self, script, *, check=True, **kwargs):
        name = 'colony-cache-container-' + uuid.uuid4().hex
        self.containers.append(name)
        try:
            return docker('run', '--rm', '--name', name, *self.options(**kwargs), IMAGE, '/bin/bash', '-euc', script, check=check)
        except subprocess.TimeoutExpired:
            docker('rm', '-f', name, check=False)
            raise

    def create(self, script, **kwargs):
        name = 'colony-cache-container-' + uuid.uuid4().hex
        docker('create', '--name', name, *self.options(**kwargs), IMAGE, '/bin/bash', '-euc', script)
        self.containers.append(name)
        return name

    def narinfos(self):
        result = self.run_in(f'find {shlex.quote(ROOT)} -name "*.narinfo" -printf "%f\\n"', enabled=False)
        return set(result.stdout.splitlines())

    def assert_cached(self, key):
        self.assertIn(self.paths[key].split('/')[3].split('-')[0] + '.narinfo', self.narinfos())

    def test_disabled_and_cold_no_seed(self):
        self.run_in('''for command in nix nix-env nix-store nix-build nix-shell nix-channel nix-copy-closure nix-instantiate nix-prefetch-url; do
            test "$(command -v "$command")" = "/usr/local/bin/$command"
            test -x "/usr/local/libexec/nix-real/$command"
        done''', enabled=False)
        result = self.run_in(f'nix --version; test ! -e "{ROOT}/binary"', enabled=False)
        self.assertIn('cache-disabled', result.stderr)
        self.run_in('nix --version; test -f "$COLONY_NIX_CACHE_DIR/binary/nix-cache-info"')
        self.assertEqual(self.narinfos(), set())

    def test_legacy_fetch_helper(self):
        result = self.run_in('nix-prefetch-url --name cached-fixture file:///fixture/cached-fixture')
        self.assertTrue(result.stdout.strip())
        self.assert_cached('fixed')

    def test_signed_offline_reuse_and_closure(self):
        path = self.paths['hello']
        start = time.monotonic()
        producer = self.run_in(f'nix-store -r {path}')
        setup = re.search(r'initialization_seconds=([\d.]+)', producer.stderr)
        export = re.search(r'export_seconds=([\d.]+)', producer.stderr)
        if setup and export:
            print(f'cold_cache_initialization_seconds={setup.group(1)} first_export_seconds={export.group(1)}', file=sys.stderr)
        self.assertEqual(producer.stdout.strip(), path)
        self.assert_cached('hello')
        consumer = self.run_in(f'nix-store -r {path}; {path}/bin/hello; nix-store --verify --check-contents', source=False)
        self.assertIn('Hello, world!', consumer.stdout)
        self.assertIn('copying 0 paths', consumer.stderr)
        sigs = self.run_in(f'nix path-info --store "file://$COLONY_NIX_CACHE_DIR/binary" --sigs {path}')
        self.assertIn('cache.nixos.org-1:', sigs.stdout)
        print(f'cold_install_and_offline_reuse_seconds={time.monotonic()-start:.3f}', file=sys.stderr)

    def test_signed_dependency_closure_missing_from_image(self):
        path = self.paths['toilet']
        # Assert the fixture includes dependencies not already in the image.
        script = f'''
import subprocess
real='/usr/local/libexec/nix-real/nix'
local=set(subprocess.check_output([real,'path-info','--all'],text=True).splitlines())
closure=set(subprocess.check_output([real,'path-info','--recursive','--store','file:///fixture/binary','{path}'],text=True).splitlines())
assert len(closure-local)>1, closure-local
subprocess.run(['nix-store','-r','{path}'],check=True)
'''
        self.run_in('python3 -c ' + shlex.quote(script))
        result = self.run_in(f'nix-store -r {path}; {path}/bin/toilet -f term closure-restored; nix-store --verify --check-contents', source=False)
        self.assertIn('closure-restored', result.stdout)

    def test_child_process_and_cli_transparency(self):
        path = self.paths['hello']
        result = self.run_in("python3 -c " + shlex.quote(
            f"import subprocess; subprocess.run(['nix-store','-r','{path}'],check=True)"))
        self.assertEqual(result.stdout.strip(), path)
        self.assert_cached('hello')
        value = 'spaces "quotes" $literal\nnewline'
        expr = '{ x = builtins.getEnv "VALUE"; }'
        result = self.run_in('nix eval --impure --json --expr ' + shlex.quote(expr), env={'VALUE': value})
        self.assertEqual(json.loads(result.stdout), {'x': value})
        result = self.run_in("printf '41 + 1' | nix-instantiate --eval -E -")
        self.assertEqual(result.stdout.strip(), '42')
        # Covered legacy import consumes stdin, unlike internal queries/exports.
        result = self.run_in('nix-store --import < /fixture/hello.export')
        self.assertEqual(result.stdout.strip(), path)
        self.assertIn('copying', result.stderr)

    def test_caller_python_environment_cannot_break_wrapper(self):
        result = self.run_in('''mkdir /tmp/shadow
            printf 'raise RuntimeError("unexpected import")\\n' > /tmp/shadow/json.py
            PYTHONHOME=/nonexistent PYTHONPATH=/tmp/shadow nix eval --json --expr 42''')
        self.assertEqual(json.loads(result.stdout), 42)

    def test_closed_stderr_keeps_json_stdout_clean(self):
        result = self.run_in('nix eval --json --expr 42 2>&-')
        self.assertEqual(json.loads(result.stdout), 42)

    def test_legacy_and_modern_commands(self):
        path = self.paths['hello']
        for command in (f'nix copy --from file:///fixture/binary {path}',
                        f'nix --option substitute true build --no-link {path}',
                        f'nix --option substitute true profile install --profile /tmp/profile {path}',
                        'nix --option substitute true run path:/fixture/flake#hello',
                        f'nix --option substitute true shell {path} -c hello'):
            with self.subTest(command=command):
                result = self.run_in(command)
                self.assertIn('copying', result.stderr)
                self.assert_cached('hello')
        # Legacy nix-env can fail when constructing its local profile, but
        # downloaded paths must still be exported without changing its status.
        result = self.run_in(f'nix-env -i {path}', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assert_cached('hello')
        expression = ('derivation { name="cached-fixture"; system=builtins.currentSystem; '
                      'builder="/bin/false"; outputHashMode="flat"; outputHashAlgo="sha256"; '
                      f'outputHash="{self.paths["hash"]}"; }}')
        result = self.run_in('nix-build --no-out-link --expr ' + shlex.quote(expression))
        self.assertEqual(result.stdout.strip(), self.paths['fixed'])
        self.assert_cached('fixed')
        expression = ('derivation { name="cache-shell"; system=builtins.currentSystem; builder="/bin/sh"; '
                      f'inputPath = builtins.appendContext "{path}" {{ "{path}".path = true; }}; }}')
        result = self.run_in('NIX_BUILD_SHELL=/bin/bash nix-shell --expr ' + shlex.quote(expression) +
                             " --run 'echo legacy-shell'")
        self.assertIn('legacy-shell', result.stdout)
        self.assert_cached('hello')

    def test_nonzero_and_nested_long_running(self):
        path = self.paths['hello']
        failed = self.run_in(f'nix --option substitute true shell {path} -c sh -c "exit 23"', check=False)
        self.assertEqual(failed.returncode, 23)
        self.assert_cached('hello')
        result = self.run_in(f'''nix --option substitute true shell {path} -c sh -c '
            nix-store -r {self.paths['figlet']}
            test -f "$COLONY_NIX_CACHE_DIR/binary/{self.paths['figlet'].split('/')[3].split('-')[0]}.narinfo"
        ' ''')
        self.assertEqual(result.returncode, 0)
        self.assert_cached('figlet')

    def test_concurrent_first_use_and_identical_installs(self):
        path = self.paths['hello']
        scripts = []
        for own, other in [('a', 'b'), ('b', 'a')]:
            scripts.append(f'''nix --option substitute true shell {path} -c sh -c '
                touch "$COLONY_NIX_CACHE_DIR/{own}"
                while ! test -f "$COLONY_NIX_CACHE_DIR/{other}"; do sleep 0.05; done
            '
            stat -c %i "$COLONY_NIX_CACHE_DIR/export.lock"
            ''')
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(self.run_in, scripts))
        self.assertEqual(results[0].stdout.strip(), results[1].stdout.strip())
        self.assertTrue(any('copying 0 paths' in r.stderr for r in results))
        self.assert_cached('hello')

    def test_concurrent_different_installs(self):
        self.run_in('nix --version')
        scripts = [f'''nix --option substitute true shell {self.paths[key]} -c sh -c '
            touch "$COLONY_NIX_CACHE_DIR/{key}"
            while ! test -f "$COLONY_NIX_CACHE_DIR/{other}"; do sleep 0.05; done
        ' ''' for key, other in [('hello', 'figlet'), ('figlet', 'hello')]]
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(self.run_in, scripts))
        self.assert_cached('hello')
        self.assert_cached('figlet')

    def test_concurrent_wrappers_in_one_container(self):
        self.run_in(f'nix-store -r {self.paths["hello"]} & a=$!; '
                    f'nix-store -r {self.paths["figlet"]} & b=$!; wait "$a"; wait "$b"')
        self.assert_cached('hello')
        self.assert_cached('figlet')

    def test_previous_miss_then_publish(self):
        path = self.paths['hello']
        reader = self.create('exec sleep 90', source=False)
        docker('start', reader)
        before = docker('exec', reader, 'nix-store', '-r', path, check=False)
        self.assertNotEqual(before.returncode, 0)
        self.run_in(f'nix-store -r {path}')
        after = docker('exec', reader, 'nix-store', '-r', path)
        self.assertEqual(after.stdout.strip(), path)

    def test_lock_timeout_pending_retry_and_failed_status(self):
        path = self.paths['hello']
        self.run_in('nix --version')
        script = f'''
import fcntl, os, subprocess
lock=open(os.environ['COLONY_NIX_CACHE_DIR']+'/export.lock','r+')
fcntl.flock(lock,fcntl.LOCK_EX)
p=subprocess.run(['nix','--option','substitute','true','shell','{path}','-c','sh','-c','exit 19'])
assert p.returncode==19, p.returncode
fcntl.flock(lock,fcntl.LOCK_UN)
subprocess.run(['nix','--version'],check=True)
'''
        result = self.run_in('python3 -c ' + shlex.quote(script), env={'COLONY_NIX_CACHE_LOCK_TIMEOUT_SECONDS':'0.2'})
        self.assertIn('lock timeout', result.stderr)
        self.assert_cached('hello')

    def test_unwritable_and_unsupported_ownership(self):
        result = self.run_in('nix eval --json --expr 42', readonly=True)
        self.assertEqual(json.loads(result.stdout), 42)
        self.assertIn('initialization failure', result.stderr)
        result = self.run_in('chown 123:123 "$COLONY_NIX_CACHE_DIR"; nix eval --json --expr 43')
        self.assertEqual(json.loads(result.stdout), 43)
        self.assertIn('initialization failure', result.stderr)

    def test_export_timeout_pending_retry(self):
        path = self.paths['hello']
        # Fault injection changes only a disposable container's internal copy
        # executable. A real child holds the inherited lock while sleeping.
        script = f'''
real=$(readlink -f /usr/local/libexec/nix-real/nix)
rm /usr/local/libexec/nix-real/nix
cat > /usr/local/libexec/nix-real/nix <<EOF
#!/bin/sh
if [ "\\$1" = copy ] && [ "\\$2" = --option ] && [ ! -e /tmp/stalled-once ]; then
    touch /tmp/stalled-once
    sleep 20
fi
exec "$real" "\\$@"
EOF
chmod +x /usr/local/libexec/nix-real/nix
COLONY_NIX_CACHE_EXPORT_TIMEOUT_SECONDS=5 nix-store -r {path}
nix --version
'''
        result = self.run_in(script)
        self.assertIn('export timeout', result.stderr)
        self.assert_cached('hello')

    def test_export_failure_pending_retry(self):
        path = self.paths['hello']
        script = f'''
real=$(readlink -f /usr/local/libexec/nix-real/nix)
rm /usr/local/libexec/nix-real/nix
cat > /usr/local/libexec/nix-real/nix <<EOF
#!/bin/sh
if [ "\\$1" = copy ] && [ "\\$2" = --option ] && [ ! -e /tmp/failed-once ]; then
    touch /tmp/failed-once
    exit 77
fi
exec "$real" "\\$@"
EOF
chmod +x /usr/local/libexec/nix-real/nix
nix-store -r {path}
nix --version
'''
        result = self.run_in(script)
        self.assertIn('export failure', result.stderr)
        self.assert_cached('hello')

    def test_corrupt_and_unsigned_cache_entries_fail_closed(self):
        path = self.paths['hello']
        narinfo = path.split('/')[3].split('-')[0] + '.narinfo'
        self.run_in(f'nix-store -r {path}')
        result = self.run_in(f"sed -i '/^Sig:/d' \"$COLONY_NIX_CACHE_DIR/binary/{narinfo}\"; nix-store -r {path}",
                             source=False, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not signed', result.stderr)
        # Restore signed metadata and corrupt the archive. No retry/replay of
        # the user command is allowed to hide an integrity failure.
        self.run_in(f'cp /fixture/binary/{narinfo} "{ROOT}/binary/{narinfo}"', enabled=False)
        result = self.run_in(f'''url=$(sed -n 's/^URL: //p' "$COLONY_NIX_CACHE_DIR/binary/{narinfo}")
printf corrupt > "$COLONY_NIX_CACHE_DIR/binary/$url"
nix-store -r {path}''', source=False, check=False)
        self.assertNotEqual(result.returncode, 0)

    def test_special_path_and_warm_overhead(self):
        path = self.paths['hello']
        result = self.run_in(f'nix-store -r {path}; nix eval --json --expr 42', root='/cache with space#percent%')
        self.assertEqual(result.stdout.splitlines()[-1], '42')
        timing = self.run_in('''python3 - <<'INNER'
import subprocess,time,json
results={}
for exe in ['/usr/local/libexec/nix-real/nix','nix']:
    start=time.monotonic()
    for _ in range(10):
        subprocess.run([exe,'eval','--json','--expr','42'],check=True,stdout=subprocess.DEVNULL)
    results[exe]=(time.monotonic()-start)/10
print(json.dumps(results))
INNER''')
        self.assertNotIn('copying', timing.stderr)
        print('warm_mean_seconds=' + timing.stdout.strip(), file=sys.stderr)

    def test_missing_output_does_not_build(self):
        result = self.run_in('''nix-build --no-out-link --option substituters '' --expr '
          derivation { name="cache-test-missing"; system=builtins.currentSystem;
          builder="/bin/sh"; args=[ "-c" "echo forbidden > $out" ]; preferLocalBuild=true; }
        ' ''', source=False, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('local builds are disabled', result.stderr)

    def test_colliding_exporter_pids(self):
        self.run_in('nix --version')
        # Use unprivileged fork padding in each disposable PID namespace to
        # give the *real* exporting Nix processes the same PID deterministically.
        # The wrapper still owns the lock and starts the exporter normally.
        shim = '''#!/root/.nix-profile/bin/python3
import os, sys
real = REAL_PATH
if sys.argv[1:3] == ['copy', '--option']:
    while True:
        pid = os.fork()
        if pid == 0:
            if os.getpid() == 2048:
                print('test-exporter-pid=2048', file=sys.stderr, flush=True)
                os.execv(real, [real, *sys.argv[1:]])
            os._exit(0)
        _, status = os.waitpid(pid, 0)
        if pid == 2048:
            sys.exit(os.waitstatus_to_exitcode(status))
        if pid > 2048:
            sys.exit(125)
os.execv(real, [real, *sys.argv[1:]])
'''
        setup = "import os, pathlib; p=pathlib.Path('/usr/local/libexec/nix-real/nix'); real=os.path.realpath(p); p.unlink(); p.write_text(" + repr(shim) + ".replace('REAL_PATH',repr(real))); p.chmod(0o755)"
        script = 'python3 -c ' + shlex.quote(setup) + f'; nix-store -r {self.paths["hello"]}'
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda command: self.run_in(command, env={
                'COLONY_NIX_CACHE_LOCK_TIMEOUT_SECONDS':'60',
                'COLONY_NIX_CACHE_EXPORT_TIMEOUT_SECONDS':'60',
            }), [script] * 2))
        pids = [re.search(r'test-exporter-pid=(\d+)', result.stderr).group(1) for result in results]
        self.assertEqual(pids[0], pids[1], f'expected colliding container-local PIDs: {pids}')
        self.assertTrue(any('copying 0 paths' in r.stderr for r in results))

    def test_interrupted_export_releases_lock_and_retains_pending(self):
        path = self.paths['hello']
        script = f'''
if [ ! -f /tmp/shim-installed ]; then
    real=$(readlink -f /usr/local/libexec/nix-real/nix)
    rm /usr/local/libexec/nix-real/nix
    cat > /usr/local/libexec/nix-real/nix <<EOF
#!/bin/sh
if [ "\\$1" = copy ] && [ "\\$2" = --option ] && [ ! -f /tmp/stalled-once ]; then
    touch /tmp/stalled-once "$ROOT/export-started"
    sleep 60
fi
exec "$real" "\\$@"
EOF
    chmod +x /usr/local/libexec/nix-real/nix
    touch /tmp/shim-installed
fi
exec nix-store -r {path}
'''.replace('$ROOT', ROOT)
        writer = self.create(script)
        docker('start', writer)
        for _ in range(200):
            if docker('exec', writer, 'test', '-f', ROOT+'/export-started', check=False).returncode == 0:
                break
            time.sleep(0.05)
        else:
            self.fail('export never started')
        docker('kill', '--signal', 'TERM', writer)
        self.assertEqual(docker('wait', writer, timeout=10).stdout.strip(), '143')
        # A different container can immediately acquire the same lock and write.
        self.run_in(f'nix-store -r {self.paths["figlet"]}')
        restarted = docker('start', '-a', writer)
        self.assertIn('copying', restarted.stderr)
        self.assert_cached('hello')
        self.run_in(f'nix-store -r {path}; {path}/bin/hello', source=False)

    def test_terminal_input_and_signal_forwarding(self):
        script = r'''
import os, pty, select, signal, subprocess, time
pid, fd = pty.fork()
if pid == 0:
    os.execvp('nix', ['nix','--option','substitute','true','shell','--expr','[]','-c','bash','--noprofile','--norc'])
output=b''
def read_until(value):
    global output
    end=time.monotonic()+10
    while value not in output and time.monotonic()<end:
        if select.select([fd],[],[],0.1)[0]:
            output+=os.read(fd,4096)
    assert value in output, repr(output)
os.write(fd,b"printf 'terminal-%s\\n' preserved\n")
read_until(b'terminal-preserved')
os.write(fd,b'exit 17\n')
_, status=os.waitpid(pid,0)
assert os.waitstatus_to_exitcode(status)==17, status
os.close(fd)
# Kill the wrapper directly, rather than relying on terminal group delivery.
p=subprocess.Popen(['nix','--option','substitute','true','shell','--expr','[]','-c','sleep','60'])
time.sleep(0.2)
p.terminate()
assert p.wait(timeout=5)==-signal.SIGTERM
'''
        self.run_in('python3 -c ' + shlex.quote(script))

    def test_cancelled_user_command_preserves_new_paths_for_retry(self):
        path = self.paths['hello']
        script = f'''exec nix --option substitute true shell {path} -c sh -c '
            if [ ! -e /tmp/ran-once ]; then
                touch /tmp/ran-once "$COLONY_NIX_CACHE_DIR/main-started"
                sleep 60
            fi
        ' '''
        writer = self.create(script)
        docker('start', writer)
        for _ in range(200):
            if docker('exec', writer, 'test', '-f', ROOT+'/main-started', check=False).returncode == 0:
                break
            time.sleep(0.05)
        else:
            self.fail('user command never started')
        docker('kill', '--signal', 'TERM', writer)
        self.assertEqual(docker('wait', writer, timeout=10).stdout.strip(), '143')
        self.assertEqual(self.narinfos(), set())
        docker('start', '-a', writer)
        self.assert_cached('hello')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', default=IMAGE)
    parser.add_argument('--platform', default=PLATFORM)
    args, remaining = parser.parse_known_args()
    IMAGE, PLATFORM = args.image, args.platform
    unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)
