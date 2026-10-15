#!/usr/bin/env python3
"""Install only checksum-verified, resolved upstream artifacts into /out."""
import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path


def download(artifact, destination):
    for attempt in range(3):
        try:
            with urllib.request.urlopen(artifact['url'], timeout=90) as response, destination.open('wb') as out:
                shutil.copyfileobj(response, out)
            break
        except (OSError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    algorithm = 'sha256' if 'sha256' in artifact else 'sha512'
    with destination.open('rb') as source:
        actual = hashlib.file_digest(source, algorithm).hexdigest()
    if actual != artifact[algorithm]:
        raise ValueError(f'Checksum mismatch: {artifact["url"]}')


def install(manifest, arch, root):
    bin_dir = root / 'usr/local/bin'
    bin_dir.mkdir(parents=True)
    for tool, data in manifest['tools'].items():
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            archive = temp / 'download'
            download(data['artifacts'][arch], archive)
            if tool == 'yq':
                shutil.copyfile(archive, bin_dir / tool)
                (bin_dir / tool).chmod(0o755)
                continue
            extracted = temp / 'extracted'
            with tarfile.open(archive) as tar:
                tar.extractall(extracted, filter='data')
            if tool in ('node', 'pnpm'):
                entries = list(extracted.iterdir())
                if len(entries) != 1 or not entries[0].is_dir():
                    raise ValueError(f'Unexpected {tool} archive layout')
                destination = root / 'opt' / tool
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(entries[0], destination, symlinks=True)
                if tool == 'node':
                    bins = {name: 'bin/' + name for name in ['node', 'npm', 'npx']}
                else:
                    package = json.loads((destination / 'package.json').read_text())
                    bins = {name: package['bin'][name] for name in ['pnpm', 'pnpx']}
                    if 'native_artifacts' in data:
                        native = temp / 'native.tgz'
                        download(data['native_artifacts'][arch], native)
                        with tarfile.open(native) as tar:
                            tar.extractall(temp / 'native', filter='data')
                        binary = temp / 'native/package/pnpm'
                        if not binary.is_file():
                            raise ValueError('Unexpected pnpm native archive layout')
                        first = destination / bins['pnpm']
                        shutil.copyfile(binary, first)
                        for name, relative in bins.items():
                            target = destination / relative
                            if name != 'pnpm' and target != first:
                                target.unlink(missing_ok=True)
                                os.link(first, target)
                    for relative in bins.values():
                        target = (destination / relative).resolve()
                        if not target.is_relative_to(destination.resolve()):
                            raise ValueError('pnpm executable escaped its package')
                        target.chmod(0o755)
                for name, relative in bins.items():
                    (bin_dir / name).symlink_to(f'/opt/{tool}/{relative}')
            else:
                for name in (['uv', 'uvx'] if tool == 'uv' else [tool]):
                    matches = [p for p in extracted.rglob(name) if p.is_file()]
                    if len(matches) != 1:
                        raise ValueError(f'Unexpected {name} archive layout')
                    shutil.copyfile(matches[0], bin_dir / name)
                    (bin_dir / name).chmod(0o755)


if __name__ == '__main__':
    install(json.loads(Path(sys.argv[1]).read_text()), sys.argv[2], Path('/out'))
