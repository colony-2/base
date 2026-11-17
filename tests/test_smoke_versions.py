import json
import unittest
from pathlib import Path
from unittest.mock import mock_open, patch


class SmokeVersionTests(unittest.TestCase):
    def check_version(self, output):
        script = (Path(__file__).parents[1] / 'scripts/smoke-test.sh').read_text()
        check = script.split("python3 - <<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
        manifest = {'tools': {'c2j': {'source': {'commit': 'a' * 40}}},
                    'nix': {'version': '2.34.3'}}
        with patch('builtins.open', mock_open(read_data=json.dumps(manifest))), \
                patch('subprocess.check_output', side_effect=[output, 'nix (Nix) 2.34.3\n']):
            exec(compile(check, 'smoke-test.sh', 'exec'), {})

    def test_source_version_matches_resolved_commit(self):
        self.check_version('c2j version v0.0.0-20261117210233-' + 'a' * 12 + '\n')

    def test_wrong_commit_release_or_dirty_checkout_fails(self):
        for version in ('v0.0.0-20261117210233-' + 'b' * 12,
                        'v0.0.75', 'dev', 'v0.0.0-20261117210233-' + 'a' * 12 + '+dirty'):
            with self.subTest(version=version), self.assertRaises(AssertionError):
                self.check_version('c2j version ' + version + '\n')
