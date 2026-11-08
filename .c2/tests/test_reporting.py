"""Fault checks for the hook reporter; no external services or candidate edits."""
import contextlib
import hashlib
import importlib.util
import io
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

    def status(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            reporter.aggregate(self.inbox, self.outbox)
        return out.getvalue()

    def test_valid_transport(self):
        self.assertEqual(self.status(), 'passed')
        self.assertTrue((self.outbox / 'environment.json').is_file())
        self.assertTrue((self.outbox / 'verification.md').is_file())

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
        out = io.StringIO()
        with patch.object(Path, 'write_text', side_effect=OSError('evidence unavailable')):
            with contextlib.redirect_stdout(out), self.assertRaises(OSError):
                reporter.aggregate(self.inbox, self.outbox)
        self.assertEqual(out.getvalue(), '')

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
