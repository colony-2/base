"""Start the real Nix as PID 1 after a cross-container test barrier."""
import json
import os
import sys
import time
from pathlib import Path

name, *command = sys.argv[1:]
coord = Path('/coord')
(coord / ('ready-' + name)).write_text(json.dumps({
    'pid': os.getpid(), 'namespace': os.readlink('/proc/self/ns/pid'),
}))
deadline = time.monotonic() + 30
while not (coord / 'go').exists():
    if time.monotonic() > deadline:
        raise SystemExit('barrier timeout')
    time.sleep(0.01)
os.execvp(command[0], command)
