"""Append only Vela's HTTPS site; validate, reload, and preserve other routes."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

CONFIG = Path('/srv/nexus/conf/Caddyfile')
SITE = '''
# Vela media downloader
https://vela.home.arpa {
    tls {
        issuer internal {
            ca nexus
        }
    }
    encode zstd gzip
    route {
        @outsideTrusted not remote_ip 192.168.1.0/24
        respond @outsideTrusted "Nur im Trusted-Netz erreichbar." 403
        reverse_proxy http://vela-api:8080
    }
}
'''
before = CONFIG.read_bytes()
text = before.decode('utf-8')
if 'https://vela.home.arpa {' in text:
    if SITE not in text:
        raise RuntimeError('existing_vela_site_conflict')
    print(json.dumps({'ok': True, 'changed': False}))
else:
    updated = before + SITE.encode('utf-8')
    pending = CONFIG.with_name('Caddyfile.vela-pending')
    if pending.exists():
        raise RuntimeError('pending_config_exists')
    backup = Path('/srv/vela/secrets/caddy-before.conf')
    if not backup.exists():
        backup.write_bytes(before)
        backup.chmod(0o600)
    pending.write_bytes(updated)
    try:
        subprocess.run(['docker', 'exec', 'nexus-proxy', 'caddy', 'validate', '--config', '/etc/caddy/' + pending.name], check=True, capture_output=True)
        if CONFIG.read_bytes() != before:
            raise RuntimeError('caddy_configuration_drift')
        os.replace(pending, CONFIG)
        try:
            subprocess.run(['docker', 'kill', '--signal', 'SIGUSR1', 'nexus-proxy'], check=True, capture_output=True)
        except Exception:
            if CONFIG.read_bytes() == updated:
                CONFIG.write_bytes(before)
                subprocess.run(['docker', 'kill', '--signal', 'SIGUSR1', 'nexus-proxy'], check=True, capture_output=True)
            raise
    finally:
        pending.unlink(missing_ok=True)
    print(json.dumps({'ok': True, 'changed': True, 'sha256': hashlib.sha256(updated).hexdigest(), 'other_routes_preserved': True}))
