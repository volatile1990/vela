"""Prepare only the new Vela-owned browser directories on the existing host."""
import os
from pathlib import Path
import stat
import subprocess


if os.getuid() != 0 or subprocess.check_output(['hostname'], text=True).strip() != 'heimserver':
    raise SystemExit('host_identity_mismatch')
base = Path('/srv/vela')
if base.is_symlink() or base.resolve() != base or not base.is_dir():
    raise SystemExit('vela_state_identity_mismatch')
for name in ('browser-control', 'browser-secrets', 'browser-profile'):
    target = base / name
    if target.is_symlink():
        raise SystemExit('browser_directory_symlink')
    target.mkdir(mode=0o700, exist_ok=True)
    info = target.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, 10020}:
        raise SystemExit('browser_directory_conflict')
    os.chown(target, 10020, 10020)
    os.chmod(target, 0o700)
print('Vela browser directories prepared')
