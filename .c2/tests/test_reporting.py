"""Fault checks for the hook reporter; no external services or candidate edits."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('reporter', Path(__file__).resolve().parents[2] / 'scripts/verify-local.py')
reporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reporter)
image_spec = importlib.util.spec_from_file_location('image_runner', Path(spec.origin).with_name('verify-image.py'))
image_runner = importlib.util.module_from_spec(image_spec)
image_spec.loader.exec_module(image_runner)
CANDIDATE = 'a' * 40
BASE = 'b' * 40


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='base-report-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inbox = self.root / 'inbox'
        self.outbox = self.root / 'outbox'
        self.inbox.mkdir()
        self.outbox.mkdir()
        for name in reporter.SUITES:
            raw = b'\nRan 1 test in 0.001s\n\nOK\n'
            (self.inbox / (name + '.log')).write_bytes(raw)
            (self.inbox / (name + '.json')).write_text(json.dumps({
                'behavior': name, 'status': 'passed', 'returncode': 0,
                'log_sha256': hashlib.sha256(raw).hexdigest()}))
        image = dict(behavior='image', candidate=CANDIDATE, base=BASE, status='passed',
                     platform='linux/arm64', fingerprint='c' * 64,
                     image=dict(revision=CANDIDATE, id='sha256:' + 'd' * 64, platform='linux/arm64'),
                     acceptance=dict(tests_run=4, passed=sorted(image_runner.REQUIRED_CASES),
                                     skipped=[dict(case='fixture_test', reason='fixture skip')]), stages={})
        for name in reporter.IMAGE_STAGES:
            raw = f'{name} completed\n'.encode()
            (self.inbox / f'image-{name}.log').write_bytes(raw)
            image['stages'][name] = dict(status='passed', returncode=0, log_sha256=hashlib.sha256(raw).hexdigest())
        (self.inbox / 'image.json').write_text(json.dumps(image))

    def status(self):
        return reporter.aggregate(self.inbox, self.outbox, CANDIDATE, BASE)

    def run_aggregate(self):
        script = self.root / 'scripts' / 'verify-local.py'
        script.parent.mkdir()
        script.write_bytes(Path(spec.origin).read_bytes())
        return subprocess.run(
            [sys.executable, '-B', str(script), 'aggregate'],
            env=dict(os.environ, INBOX=str(self.inbox), OUTBOX=str(self.outbox), CANDIDATE=CANDIDATE, BASE=BASE),
            capture_output=True, text=True,
        )

    def test_valid_transport(self):
        self.assertEqual(self.status(), 'passed')
        self.assertTrue((self.outbox / 'environment.json').is_file())
        self.assertTrue((self.outbox / 'verification.md').is_file())
        report = (self.outbox / 'verification.md').read_text()
        self.assertIn('1 skipped', report)
        self.assertIn('Skipped `fixture_test`: fixture skip', report)

    def test_image_log_required_and_verified(self):
        log = self.inbox / 'image-build.log'
        log.write_text('changed')
        self.assertEqual(self.status(), 'failed')
        self.assertIn('corrupt log', (self.outbox / 'verification.md').read_text())
        log.unlink()
        self.assertEqual(self.status(), 'failed')

    def test_image_revision_must_match(self):
        path = self.inbox / 'image.json'
        image = json.loads(path.read_text())
        image['image']['revision'] = BASE
        path.write_text(json.dumps(image))
        self.assertEqual(self.status(), 'failed')
        self.assertIn('matching image identity', (self.outbox / 'verification.md').read_text())

    def test_stage_failure_skips_later_checks_and_retains_logs(self):
        for name in ('preflight', 'resolve', 'build', 'accept'):
            with self.subTest(stage=name):
                runner = image_runner.ImageVerification(self.root, self.outbox, CANDIDATE, BASE)
                with patch.multiple(runner, preflight=lambda _: None, resolve=lambda: None,
                                    build=lambda: None, accept=lambda: None):
                    with patch.object(runner, name, side_effect=subprocess.TimeoutExpired(name, 1)):
                        runner.run()
                record = json.loads((self.outbox / 'image.json').read_text())
                stage = {'resolve': 'resolution', 'accept': 'acceptance'}.get(name, name)
                self.assertEqual(record['status'], 'failed')
                self.assertEqual(record['stages'][stage]['status'], 'failed')
                self.assertIn('timed out', (self.outbox / f'image-{stage}.log').read_text())
                if name != 'accept':
                    self.assertEqual(record['stages']['acceptance']['status'], 'not run')

    def test_native_load_avoids_registry_without_changing_release_modes(self):
        build = self.root / '.build'
        build.mkdir()
        (build / 'build-args.env').write_text('UBUNTU_IMAGE=ubuntu@fixture\n')
        docker = self.root / 'docker'
        docker.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        docker.chmod(0o755)
        script = Path(spec.origin).with_name('build-image.sh')
        env = dict(os.environ, PATH=str(self.root) + ':' + os.environ['PATH'], IMAGE='registry/base',
                   GITHUB_RUN_ID='17', GITHUB_RUN_ATTEMPT='1', GITHUB_SHA=CANDIDATE,
                   GITHUB_REPOSITORY='colony-2/base')
        for arch in ('amd64', 'arm64'):
            for mode in ('verify-load', 'load', 'push'):
                with self.subTest(arch=arch, mode=mode):
                    result = subprocess.run(
                        ['bash', str(script), arch, mode, 'base-verify-' + 'e' * 32 + '-candidate:local', CANDIDATE],
                        cwd=self.root, env=env, capture_output=True, text=True, check=True)
                    tokens = result.stdout.splitlines()
                    self.assertIn(f'linux/{arch}', tokens)
                    self.assertIn(f'org.opencontainers.image.revision={CANDIDATE}', tokens)
                    self.assertEqual('--load' in tokens, mode != 'push')
                    self.assertEqual('--cache-from' in tokens, mode != 'verify-load')
                    self.assertEqual('--cache-to' in tokens, mode != 'verify-load')
                    self.assertEqual(any('push=true' in token for token in tokens), mode == 'push')
        result = subprocess.run(['bash', str(script), 'arm64', 'verify-load', 'registry/image', CANDIDATE],
                                cwd=self.root, env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')

    def test_noisy_unlisted_test_prevents_pass_without_polluting_status(self):
        tests = self.root / 'tests'
        tests.mkdir()
        (tests / 'test_new_suite.py').write_text(
            'import os\n'
            'import unittest\n'
            'print("fixture loaded")\n'
            'os.write(1, b"fixture wrote to stdout\\n")\n'
            'class NewSuite(unittest.TestCase):\n'
            '    def test_new_behavior(self):\n'
            '        raise AssertionError("discovery must not run this test")\n'
        )
        result = self.run_aggregate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'failed')
        self.assertIn('fixture loaded', result.stderr)
        self.assertIn('fixture wrote to stdout', result.stderr)
        report = (self.outbox / 'verification.md').read_text()
        self.assertIn('Tests discovered by CI but not covered', report)
        self.assertIn('test_new_suite.NewSuite.test_new_behavior', report)

    def test_missing_log(self):
        (self.inbox / 'resolution.log').unlink()
        self.assertEqual(self.status(), 'failed')

    def test_corrupt_log(self):
        (self.inbox / 'resolution.log').write_text('altered')
        self.assertEqual(self.status(), 'failed')

    def test_malformed_result(self):
        (self.inbox / 'resolution.json').write_text('{broken')
        self.assertEqual(self.status(), 'failed')

    def test_missing_result(self):
        (self.inbox / 'resolution.json').unlink()
        self.assertEqual(self.status(), 'failed')

    def test_bad_result_shape(self):
        (self.inbox / 'resolution.json').write_text('null')
        self.assertEqual(self.status(), 'failed')

    def test_inconsistent_status(self):
        p = self.inbox / 'resolution.json'
        data = json.loads(p.read_text())
        data['returncode'] = 9
        p.write_text(json.dumps(data))
        self.assertEqual(self.status(), 'failed')

    def test_report_write_failure_emits_no_pass(self):
        self.outbox.rmdir()
        result = self.run_aggregate()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertIn('verification.md', result.stderr)

    def test_native_launch_error(self):
        with patch.object(sys, 'executable', str(self.root / 'missing-python')):
            reporter.run_suite('resolution', self.outbox)
        data = json.loads((self.outbox / 'resolution.json').read_text())
        self.assertEqual(data['status'], 'failed')
        self.assertIn('error', data)

    def test_native_timeout(self):
        sleeper = self.root / 'sleep-python'
        sleeper.write_text('#!' + sys.executable + '\nimport time\ntime.sleep(2)\n')
        sleeper.chmod(0o755)
        real_run = subprocess.run
        def short_run(*args, **kwargs):
            kwargs['timeout'] = 0.05
            return real_run(*args, **kwargs)
        with patch.object(sys, 'executable', str(sleeper)), patch.object(subprocess, 'run', side_effect=short_run):
            reporter.run_suite('resolution', self.outbox)
        self.assertEqual(json.loads((self.outbox / 'resolution.json').read_text())['status'], 'failed')

    def test_cancellation_not_swallowed(self):
        with patch.object(subprocess, 'run', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            reporter.run_suite('resolution', self.outbox)
        self.assertFalse((self.outbox / 'resolution.json').exists())

    def test_zero_tests_prevents_pass(self):
        def empty_run(*args, **kwargs):
            kwargs['stdout'].write(b'\nRan 0 tests in 0.001s\n\nOK\n')
            return subprocess.CompletedProcess(args[0], 0)
        with patch.object(subprocess, 'run', side_effect=empty_run):
            reporter.run_suite('resolution', self.outbox)
        self.assertEqual(json.loads((self.outbox / 'resolution.json').read_text())['status'], 'failed')


if __name__ == '__main__':
    unittest.main(verbosity=2)
