#!/usr/bin/env python3
"""Install resolved artifacts and build c2j from its pinned source commit."""
import hashlib
import json
import os
import re
import shutil
import subprocess
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


def build_c2j(data, arch, destination, temp):
    commit = data['source']['commit']
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('Invalid c2j commit')
    archive = temp / 'go.tar.gz'
    download(data['toolchain']['artifacts'][arch], archive)
    with tarfile.open(archive) as tar:
        tar.extractall(temp, filter='data')
    source = temp / 'source'
    source.mkdir()
    for args in (['init', '.'], ['remote', 'add', 'origin', data['source']['repository']],
                 ['fetch', '--depth=1', 'origin', commit], ['checkout', '--detach', commit]):
        subprocess.run(['git', *args], cwd=source, check=True)
    actual = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
    if actual != commit:
        raise ValueError('c2j checkout does not match the resolved commit')
    env = {**os.environ, 'PATH': f'{temp}/go/bin:' + os.environ['PATH'],
           'GOROOT': str(temp / 'go'), 'GOTOOLCHAIN': 'local',
           'CGO_ENABLED': '0', 'GOOS': 'linux', 'GOARCH': arch,
           'GOMODCACHE': '/root/.cache/c2j-modules', 'GOCACHE': '/root/.cache/c2j-build'}
    subprocess.run(['go', 'build', '-mod=readonly', '-trimpath', '-buildvcs=true',
                    '-o', str(destination), './cmd/c2j'], cwd=source, env=env, check=True)
    destination.chmod(0o755)


def install(manifest, arch, root):
    bin_dir = root / 'usr/local/bin'
    bin_dir.mkdir(parents=True, exist_ok=True)
    for tool, data in manifest['tools'].items():
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            if tool == 'c2j':
                build_c2j(data, arch, bin_dir / tool, temp)
                continue
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
    install(json.loads(Path(sys.argv[1]).read_text()), sys.argv[2], Path('/'))
