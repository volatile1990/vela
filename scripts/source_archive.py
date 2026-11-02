"""Produce a source-only deployment tar without local state or credentials."""
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'output' / 'vela-source.tar'
OUTPUT.parent.mkdir(exist_ok=True)
SKIP = {'.git', '.venv', 'node_modules', 'dist', '__pycache__', '.pytest_cache', '.ruff_cache', 'output', 'test-results', 'playwright-report', 'credentials', 'browser-profile', 'browser-secrets', 'browser-control'}
with tarfile.open(OUTPUT, 'w') as archive:
    for path in sorted(ROOT.rglob('*')):
        relative = path.relative_to(ROOT)
        if any(part in SKIP or part.endswith('.egg-info') for part in relative.parts) or path.is_symlink() or not path.is_file():
            continue
        if (path.suffix in {'.dpapi', '.env', '.pem', '.key', '.pyc', '.tsbuildinfo', '.cookies', '.enc', '.sock'}
            or '.sqlite' in path.name or path.name == 'cookies.txt' or path.name.endswith('.cookies.txt')):
            continue
        archive.add(path, arcname=relative.as_posix(), recursive=False)
print(OUTPUT)
