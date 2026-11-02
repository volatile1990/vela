"""Run on heimserver to build the exact tested Linux/amd64 Orbit payload."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile


def main():
    if os.getuid() != 0:
        raise SystemExit('root required')
    source = Path('/opt/vela/current').resolve()
    output = Path('/home/markus/vela-upload')
    bundle = output / 'bundle-1.0.0'
    if bundle.exists():
        raise SystemExit('Existing bundle directory: inspect before rebuilding')
    bundle.mkdir()
    images = {}
    for name in ('vela-api:1.0.0', 'vela-worker:1.0.0', 'vela-browser:1.0.0'):
        images[name] = subprocess.run(['docker', 'image', 'inspect', name, '--format', '{{.Id}}'],
            check=True, capture_output=True, text=True).stdout.strip()
    with gzip.open(bundle / 'images.tar.gz', 'wb', compresslevel=2) as archive:
        process = subprocess.Popen(['docker', 'save', *images], stdout=subprocess.PIPE)
        shutil.copyfileobj(process.stdout, archive, 1024*1024)
        if process.wait():
            raise RuntimeError('docker_save_failed')
    shutil.copytree(source, bundle / 'source', ignore=shutil.ignore_patterns(
        '__pycache__', '.pytest_cache', '.git', 'node_modules', 'dist', '*.tsbuildinfo'))
    shutil.copy2(source / 'deploy/install.sh', bundle / 'install.sh')
    (bundle / 'install.sh').chmod(0o755)
    manifest = {'app': 'vela', 'version': '1.0.0', 'platform': 'linux', 'arch': 'x86_64',
                'url': 'https://vela.home.arpa', 'images': images,
                'downloads': r'\\unas.home.arpa\Unprotected\Vela'}
    (bundle / 'release.json').write_text(json.dumps(manifest, indent=2) + '\n')
    sums = []
    for file in sorted(bundle.rglob('*')):
        if file.is_file():
            digest = hashlib.file_digest(file.open('rb'), 'sha256').hexdigest()
            sums.append(digest + '  ' + file.relative_to(bundle).as_posix())
    (bundle / 'SHA256SUMS').write_text('\n'.join(sums) + '\n')
    target = output / 'vela-1.0.0-linux-x86_64.tar.gz'
    with tarfile.open(target, 'w:gz', compresslevel=1) as archive:
        archive.add(bundle, arcname='vela-1.0.0', recursive=True)
    digest = hashlib.file_digest(target.open('rb'), 'sha256').hexdigest()
    orbit = {'app': 'vela', 'version': '1.0.0', 'platform': 'linux', 'arch': 'x86_64',
             'file': target.name, 'size': target.stat().st_size, 'sha256': digest}
    (output / 'orbit-release.json').write_text(json.dumps(orbit, indent=2) + '\n')
    print(json.dumps({'ok': True, 'manifest': orbit, 'images': images}))


if __name__ == '__main__':
    main()
