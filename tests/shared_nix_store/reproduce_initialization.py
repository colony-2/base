"""Reproduce the schema publication race without network access or fixtures.

Exit 1 means the shared-store contract is not met. No store bytes or syscall
results are injected: strace only extends the real schema creation window.
"""
import argparse
import sys

from probe import docker, name


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', default='colony-base:shared-store-probe')
    parser.add_argument('--platform', default='linux/arm64')
    args = parser.parse_args()
    volume = name()
    containers = []
    docker('volume', 'create', volume)
    common = ['--platform', args.platform, '--network', 'none', '--mount',
              f'type=volume,src={volume},dst=/nix,volume-nocopy']
    try:
        observer, writer = name(), name()
        containers.extend([observer, writer])
        docker('run', '-d', '--name', observer, *common, args.image, 'sleep', '90')
        docker('run', '-d', '--name', writer, *common, args.image,
               'strace', '-f', '-P', '/nix/var/nix/db/schema', '-e', 'trace=openat',
               '-e', 'inject=openat:delay_exit=30s', 'nix-store', '--init')
        observed = docker('exec', observer, 'python3', '-c', '''
from pathlib import Path
import time
p=Path('/nix/var/nix/db/schema')
deadline=time.monotonic()+15
while not p.exists():
    if time.monotonic()>deadline: raise SystemExit('schema creation not observed')
    time.sleep(0.01)
assert p.stat().st_size==0, 'did not pause in the intended window'
print('observed schema: 0 bytes')
''', check=False)
        if observed.returncode:
            print('Syscall pause was not established; this run cannot test the race.\n' + observed.stderr)
            return 77
        outcomes = []
        for phase in ('while initializer is paused', 'after initializer is killed'):
            if outcomes:
                docker('kill', writer)
                docker('wait', writer)
            reader = name()
            containers.append(reader)
            result = docker('run', '--rm', '--name', reader, *common, args.image,
                            'nix-store', '--init', check=False)
            print(f'{phase}: exit {result.returncode}\n{result.stderr}', flush=True)
            outcomes.append(result.returncode)
        return 1 if any(outcomes) else 0
    finally:
        for container in containers:
            docker('rm', '-f', container, check=False)
        docker('volume', 'rm', volume)


if __name__ == '__main__':
    sys.exit(main())
