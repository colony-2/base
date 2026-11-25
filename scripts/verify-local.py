#!/usr/bin/env python3
"""Run one offline suite, or strictly aggregate its transported evidence."""
from collections import Counter
import hashlib
import json
import os
import platform
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SUITES = {
    'resolution': 'test_resolve_versions.py',
    'publication_planning': 'test_publish_image.py',
    'fast_resolution': 'test_fast_resolution.py',
    'smoke_versions': 'test_smoke_versions.py',
}
IMAGE_STAGES = ('preflight', 'resolution', 'build', 'acceptance', 'cleanup')
HELD = ('Verification covers one native architecture. Image publication, signing, '
        'and remote release CI are not run.')


def run_suite(name, outbox):
    command = [sys.executable, '-B', '-m', 'unittest', 'discover', '-s',
               str(ROOT / 'tests'), '-p', SUITES[name], '-v']
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


def test_ids(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from test_ids(test)
        else:
            yield test.id()


def discovery_errors():
    """Compare what CI discovers with what the named local suites discover."""
    tests = str(ROOT / 'tests')
    loader = unittest.TestLoader()
    try:
        ci_tests = Counter(test_ids(loader.discover(tests)))
        if loader.errors:
            return ['CI test discovery failed:', *loader.errors]

        local_tests = Counter()
        for pattern in SUITES.values():
            local_tests.update(test_ids(loader.discover(tests, pattern=pattern)))
        if loader.errors:
            return ['Local suite discovery failed:', *loader.errors]
    except (ImportError, OSError) as error:
        return ['Cannot check test discovery: ' + str(error)]

    missing = ci_tests - local_tests
    if missing:
        return ['Tests discovered by CI but not covered by the Base verify suites:',
                *sorted(missing)]
    return []


def read_image(inbox, candidate, base):
    record = json.loads((inbox / 'image.json').read_text())
    if (record['behavior'] != 'image' or record['candidate'] != candidate or record['base'] != base
            or not re.fullmatch(r'[0-9a-f]{40}', candidate or '')
            or set(record['stages']) != set(IMAGE_STAGES)):
        raise ValueError('Invalid image result or candidate/base mismatch')
    for name in IMAGE_STAGES:
        stage = record['stages'][name]
        raw = (inbox / f'image-{name}.log').read_bytes()
        if (not raw or stage['log_sha256'] != hashlib.sha256(raw).hexdigest()
                or stage['status'] not in ('passed', 'failed', 'not run')):
            raise ValueError(f'Invalid {name} result or missing/corrupt log')
    if record['status'] == 'passed':
        image = record['image']
        if (any(stage['status'] != 'passed' or stage.get('returncode') != 0
                for stage in record['stages'].values())
                or image['revision'] != candidate or image['platform'] != record['platform']
                or record['platform'] not in ('linux/amd64', 'linux/arm64')
                or not re.fullmatch(r'sha256:[0-9a-f]{64}', image['id'])
                or not re.fullmatch(r'[0-9a-f]{64}', record['fingerprint'])
                or not isinstance(record['acceptance']['skipped'], list)):
            raise ValueError('Image pass lacks successful stages or matching image identity')
    elif record['status'] != 'failed':
        raise ValueError('Invalid image status')
    return record


def aggregate(inbox, outbox, candidate=None, base=None):
    records = []
    errors = discovery_errors()
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
    image = None
    try:
        image = read_image(inbox, candidate, base)
        records.append(image)
        if image['status'] != 'passed':
            errors.append('image: ' + ', '.join(f'{name} {stage["status"]}'
                          for name, stage in image['stages'].items() if stage['status'] != 'passed'))
    except (OSError, ValueError, KeyError, TypeError) as error:
        errors.append('image: ' + str(error))
    report = '# Base local verification\n\n' + '\n'.join(
        f"- {r['behavior']}: {r['status']}" for r in records)
    report += '\n\n' + HELD + '\n'
    if image:
        report += '\n' + '\n'.join(f"- Image {name}: {stage['status']}"
                                    for name, stage in image['stages'].items()) + '\n'
        if identity := image.get('image'):
            report += f"\nLocal image `{identity['id']}` ({identity['platform']}, `{identity['revision']}`).\n"
        if acceptance := image.get('acceptance'):
            skipped = acceptance['skipped']
            report += f"\nAcceptance: {acceptance['tests_run']} tests run, {len(skipped)} skipped.\n"
            for skip in skipped:
                report += f"- Skipped `{skip['case']}`: {skip['reason']}\n"
    if errors:
        report += '\nErrors:\n' + '\n'.join(errors) + '\n'
    (outbox / 'verification.md').write_text(report)
    (outbox / 'environment.json').write_text(json.dumps(records, indent=2) + '\n')
    # Evidence writes must succeed before emitting a successful status.
    return 'failed' if errors else 'passed'


if __name__ == '__main__':
    destination = Path(os.environ['OUTBOX'])
    if sys.argv[1] == 'aggregate':
        with os.fdopen(os.dup(sys.stdout.fileno()), 'w') as status_output:
            os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
            status_output.write(aggregate(Path(os.environ['INBOX']), destination,
                                          os.environ.get('CANDIDATE'), os.environ.get('BASE')))
    else:
        run_suite(sys.argv[1], destination)
