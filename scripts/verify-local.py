#!/usr/bin/env python3
"""Run one offline suite, or strictly aggregate its transported evidence."""
import hashlib
import json
import os
import platform
from pathlib import Path
import subprocess
import sys
import tempfile

SUITES = {
    'resolution': 'test_resolve_versions.py',
    'publication_planning': 'test_publish_image.py',
    'fast_resolution': 'test_fast_resolution.py',
}
HELD = ('Real image/input resolution, Docker/buildx/QEMU, image reuse probes, '
        'shared-Nix-store container acceptance, registry authentication, all '
        'image builds, publication, signing, and remote CI are held.')


def run_suite(name, outbox):
    root = Path(__file__).resolve().parents[1]
    command = [sys.executable, '-B', '-m', 'unittest', 'discover', '-s',
               str(root / 'tests'), '-p', SUITES[name], '-v']
    log = outbox / (name + '.log')
    record = dict(behavior=name, command=command, candidate=os.environ.get('CANDIDATE'),
                  base=os.environ.get('BASE'), platform=platform.platform(),
                  machine=platform.machine(), python=sys.version, status='failed')
    # Temporary working directories isolate accidental relative output as well as
    # the fixtures' own temporary directories; imports remain candidate imports.
    with tempfile.TemporaryDirectory(prefix='base-verify-') as scratch:
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', TMPDIR=scratch,
                   XDG_CACHE_HOME=scratch, PYTHONPYCACHEPREFIX=scratch)
        with log.open('wb') as output:
            try:
                result = subprocess.run(command, cwd=scratch, env=env, stdout=output,
                                        stderr=subprocess.STDOUT, timeout=180)
                record['returncode'] = result.returncode
                record['status'] = 'passed' if result.returncode == 0 else 'failed'
            except (OSError, subprocess.TimeoutExpired) as error:
                output.write((str(error) + '\n').encode())
                record['error'] = str(error)
    raw = log.read_bytes()
    # unittest's zero-test success is not meaningful verification.
    if not raw or b'\nRan 0 tests' in raw or b'\nRan ' not in raw:
        record['status'] = 'failed'
        record['error'] = 'Missing evidence of executed tests'
    record['log_sha256'] = hashlib.sha256(raw).hexdigest()
    (outbox / (name + '.json')).write_text(json.dumps(record, indent=2) + '\n')
    # An ordinary suite failure is data, allowing the other suites to run.


def aggregate(inbox, outbox):
    records = []
    errors = []
    for name in SUITES:
        try:
            record = json.loads((inbox / (name + '.json')).read_text())
            raw = (inbox / (name + '.log')).read_bytes()
            if (record['behavior'] != name or record['status'] not in ('passed', 'failed')
                    or not raw or record['log_sha256'] != hashlib.sha256(raw).hexdigest()):
                raise ValueError('invalid result or missing/corrupt log')
            if record['status'] != 'passed' or record.get('returncode') != 0:
                errors.append(name + ': check failed')
            records.append(record)
        except (OSError, ValueError, KeyError, TypeError) as error:
            errors.append(name + ': ' + str(error))
    status = 'failed' if errors else 'passed'
    report = '# Base local verification\n\n' + '\n'.join(
        f"- {r['behavior']}: {r['status']}" for r in records)
    report += '\n\n' + HELD + '\n'
    if errors:
        report += '\nErrors:\n' + '\n'.join(errors) + '\n'
    (outbox / 'verification.md').write_text(report)
    (outbox / 'environment.json').write_text(json.dumps(records, indent=2) + '\n')
    # Evidence writes must succeed before emitting a successful status.
    print(status, end='')


if __name__ == '__main__':
    destination = Path(os.environ['OUTBOX'])
    if sys.argv[1] == 'aggregate':
        aggregate(Path(os.environ['INBOX']), destination)
    else:
        run_suite(sys.argv[1], destination)
