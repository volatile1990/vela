"""Run as root on heimserver. Create only Vela mounts, directories and units."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def write_owned(path: str, content: str):
    target = Path(path)
    if target.exists() and target.read_text() != content:
        raise RuntimeError('existing_unit_conflict:' + target.name)
    if not target.exists():
        with target.open('x') as stream:
            stream.write(content)


def main():
    if os.getuid() != 0 or run('hostname').strip() != 'heimserver':
        raise RuntimeError('host_identity_mismatch')
    base = Path('/srv/vela')
    base.mkdir(mode=0o750, exist_ok=True)
    if base.is_symlink():
        raise RuntimeError('state_root_symlink')
    os.chown(base, 0, 10020)
    for name in ('state', 'work', 'library', 'secrets', 'credentials', 'browser-control', 'browser-secrets', 'browser-profile'):
        p = base / name
        p.mkdir(mode=0o750, exist_ok=True)
        if p.is_symlink():
            raise RuntimeError('owned_path_symlink')
    for name in ('state', 'work'):
        os.chown(base / name, 10020, 10020)
    os.chown(base / 'secrets', 10020, 10020)
    os.chmod(base / 'secrets', 0o700)
    os.chown(base / 'credentials', 10020, 10020)
    os.chmod(base / 'credentials', 0o700)
    for name in ('browser-control', 'browser-secrets', 'browser-profile'):
        os.chown(base / name, 10020, 10020)
        os.chmod(base / name, 0o700)
    image = base / 'work.ext4'
    if not image.exists():
        fd = os.open(image, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.truncate(16 * 1024**3)
        run('/usr/sbin/mkfs.ext4', '-F', '-m', '0', '-L', 'vela-work', str(image))
    elif image.is_symlink() or image.stat().st_size != 16 * 1024**3:
        raise RuntimeError('existing_work_image_conflict')
    write_owned('/etc/systemd/system/srv-vela-work.mount', '''[Unit]
Description=Vela bounded 16 GiB work volume
Before=vela.service
[Mount]
What=/srv/vela/work.ext4
Where=/srv/vela/work
Type=ext4
Options=loop,nodev,nosuid,noexec
TimeoutSec=30
[Install]
WantedBy=multi-user.target
''')
    write_owned('/etc/systemd/system/srv-vela-library.mount', '''[Unit]
Description=Vela UNAS Unprotected share
Wants=network-online.target
After=network-online.target
Before=vela.service
[Mount]
What=192.168.1.88:/var/nfs/shared/Unprotected
Where=/srv/vela/library
Type=nfs
Options=vers=3,proto=tcp,hard,timeo=50,retrans=2,nodev,nosuid,noexec
TimeoutSec=30
[Install]
WantedBy=multi-user.target
''')
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', '--now', 'srv-vela-work.mount', 'srv-vela-library.mount')
    actual = run('findmnt', '-n', '-o', 'SOURCE,FSTYPE', '--mountpoint', '/srv/vela/library').split()
    if actual != ['192.168.1.88:/var/nfs/shared/Unprotected', 'nfs']:
        raise RuntimeError('nas_mount_identity_mismatch')
    if run('findmnt', '-n', '-o', 'FSTYPE', '--mountpoint', '/srv/vela/work').strip() != 'ext4':
        raise RuntimeError('work_filesystem_mismatch')
    os.chown(base / 'work', 10020, 10020)
    if Path('/srv/vela/library/Vela').is_symlink():
        raise RuntimeError('download_directory_symlink')
    run('setpriv', '--reuid=10020', '--regid=10020', '--clear-groups',
        'mkdir', '-p', '/srv/vela/library/Vela')
    # Only this application's own subfolder is touched, never the share root.
    run('setpriv', '--reuid=10020', '--regid=10020', '--clear-groups',
        'chmod', '2770', '/srv/vela/library/Vela')
    marker = Path('/srv/vela/library/Vela/.vela-volume')
    expected = 'vela-unas-unprotected-698f83b4-014e-482f-8aa1-d5a75e65b39a\n'
    if marker.exists() and marker.read_text() != expected:
        raise RuntimeError('existing_volume_marker_conflict')
    if not marker.exists():
        process = subprocess.run(['setpriv', '--reuid=10020', '--regid=10020', '--clear-groups',
            'python3', '-c', "from pathlib import Path; Path('/srv/vela/library/Vela/.vela-volume').write_text(input()+'\\n')"],
            input=expected, text=True, capture_output=True, check=True)
    print(json.dumps({'ok': True, 'mounts': ['srv-vela-work.mount', 'srv-vela-library.mount'],
        'work_limit_bytes': image.stat().st_size, 'download_share': r'\\unas.home.arpa\Unprotected\Vela',
        'marker_verified': marker.read_text() == expected}))


if __name__ == '__main__':
    main()
