"""Prototype of provider attachment validation, NOT a Nix wrapper.

Checks a fixed number of directories and a temporary write; it does not open the
Nix database or initialize a store. Provider compatibility/lease checks are not
implemented here. Cooperating workloads must not change ownership after attach.
"""
import os
import stat
import sys
import tempfile
from pathlib import Path


def check(root=Path('/nix')):
    if os.geteuid() != 0:
        raise ValueError('root is required')
    for relative in ('', 'store', 'var', 'var/nix', 'var/nix/db'):
        path = root / relative
        try:
            info = path.lstat()
        except FileNotFoundError:
            if not relative:
                raise ValueError('store mount is missing') from None
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f'{path}: expected a real directory')
        if (info.st_uid, info.st_gid) != (0, 0) or info.st_mode & 0o022:
            raise ValueError(f'{path}: expected root:root without group/world write')
        # A mode check alone is insufficient for root or a read-only mount.
        if not info.st_mode & stat.S_IWUSR:
            raise ValueError(f'{path}: owner write permission missing')
        fd, filename = tempfile.mkstemp(prefix='.colony-preflight-', dir=path)
        try:
            os.write(fd, b'probe\n')
            os.fsync(fd)
        finally:
            os.close(fd)
            os.unlink(filename)


if __name__ == '__main__':
    try:
        check()
    except (OSError, ValueError) as error:
        print(f'store preflight rejected attachment: {error}', file=sys.stderr)
        sys.exit(1)
    print('store preflight accepted attachment')
