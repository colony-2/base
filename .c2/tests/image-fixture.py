"""Produce synthetic image evidence for the native caller transport tests only."""
import hashlib
import json
import os
from pathlib import Path

outbox = Path(os.environ['OUTBOX'])
candidate, base = os.environ['CANDIDATE'], os.environ['BASE']
stages = {}
for stage in ('preflight', 'resolution', 'build', 'acceptance', 'cleanup'):
    output = f'Fixture only: {stage}\n'.encode()
    (outbox / f'image-{stage}.log').write_bytes(output)
    stages[stage] = dict(status='passed', returncode=0, log_sha256=hashlib.sha256(output).hexdigest())
(outbox / 'image.json').write_text(json.dumps(dict(
    behavior='image', candidate=candidate, base=base, platform='linux/arm64',
    status='passed', fingerprint='a' * 64, stages=stages,
    image=dict(id='sha256:' + 'b' * 64, platform='linux/arm64', revision=candidate),
    acceptance=dict(tests_run=3, passed=['test_empty_tools_and_entrypoint',
                                        'test_signed_cold_and_offline_warm', 'test_c2j_child_process'], skipped=[]),
)))
