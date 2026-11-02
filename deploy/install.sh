#!/bin/sh
set -eu
# Vela's Orbit bundle updates its prepared Heimserver installation only.
[ "$(id -u)" = 0 ] || { echo 'Als root ausführen.' >&2; exit 1; }
[ "$(hostname)" = heimserver ] || { echo 'Dieses Paket ist für den vorbereiteten Heimserver bestimmt.' >&2; exit 1; }
bundle_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$bundle_dir"
sha256sum -c SHA256SUMS
gzip -dc images.tar.gz | docker load
python3 - <<'PY'
import json,subprocess
from pathlib import Path
manifest=json.loads(Path('release.json').read_text())
for name,expected in manifest['images'].items():
 actual=subprocess.run(['docker','image','inspect',name,'--format','{{.Id}}'],capture_output=True,text=True,check=True).stdout.strip()
 if actual!=expected:raise SystemExit('Image-Prüfung fehlgeschlagen: '+name)
PY
/usr/bin/python3 source/deploy/start_guard.py api
systemctl stop vela-api.service vela-worker.service
release_dir="/opt/vela/releases/1.0.0-orbit-$(sha256sum release.json | cut -c1-12)"
if [ ! -d "$release_dir" ]; then
    mkdir -m 755 "$release_dir"
    cp -a source/. "$release_dir/"
fi
ln -s "$release_dir" /opt/vela/current.orbit-new
mv -Tf /opt/vela/current.orbit-new /opt/vela/current
python3 /opt/vela/current/deploy/install_units.py
systemctl start vela-api.service vela-worker.service
ready=0
for attempt in $(seq 1 30); do
    if docker exec vela-api python -c "import urllib.request; r=urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8080/api/health',headers={'Host':'vela.home.arpa'}),timeout=2); assert r.status==200" >/dev/null 2>&1 \
       && docker exec vela-worker python -m vela.cli health worker >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 1
done
[ "$ready" = 1 ] || { echo 'Startprüfung fehlgeschlagen; Vela-Dienste und Journal prüfen.' >&2; exit 1; }
echo 'Vela 1.0.0 ist unter https://vela.home.arpa erreichbar.'
