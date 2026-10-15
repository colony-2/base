#!/usr/bin/env python3
"""Check that the installed APT closure exactly matches the resolved inputs."""
import json
import subprocess
import sys
from pathlib import Path

expected = dict(line.split('=', 1) for line in Path(sys.argv[1]).read_text().splitlines())
arch = subprocess.check_output(['dpkg', '--print-architecture'], text=True).strip()
raw = subprocess.check_output(['dpkg-query', '-W', '-f=${Package}=${Version}\n'], text=True)
actual = dict(line.split('=', 1) for line in raw.splitlines())
if actual != expected:
    differing = {p: {'expected': expected.get(p), 'actual': actual.get(p)}
                 for p in actual.keys() | expected.keys() if actual.get(p) != expected.get(p)}
    raise SystemExit('Installed APT closure differs: ' + json.dumps(differing))
out = Path('/usr/local/share/base')
out.mkdir(parents=True, exist_ok=True)
(out / 'packages.json').write_text(json.dumps(actual, indent=2, sort_keys=True) + '\n')
