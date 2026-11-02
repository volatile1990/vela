"""Run via pinned SSH on UNAS: add only the existing Unprotected share to host NFS.

Read-only without --apply --expected-sha256. Uses the root-local native Drive
API, preserves every other share and NFS rule, never exports credentials.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.request

SHARES = '/api/v1/shared?includeNFSIsolated=true'
ADVANCED = '/api/v1/services/nfs/advanced-settings'
CLIENT = '192.168.1.175'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--expected-sha256')
    args = parser.parse_args()
    if os.getuid() != 0:
        raise RuntimeError('root_required')
    identity = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'unifi-drive', '-Atc',
        "SELECT id FROM users WHERE username='admin'"], capture_output=True, text=True, check=True).stdout.strip()
    if not re.fullmatch(r'[a-zA-Z0-9-]{8,100}', identity):
        raise RuntimeError('native_admin_identity_invalid')

    def api(path, method='GET', body=None):
        req = urllib.request.Request('http://127.0.0.1:16080' + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={'X-UserId': identity, 'X-UserRole': 'admin', 'X-Username': 'admin',
                     'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=40) as response:
            raw = response.read()
        if not raw:
            if method == 'GET':
                raise RuntimeError('native_empty_get')
            return None
        obj = json.loads(raw)
        if isinstance(obj, dict) and obj.get('err'):
            raise RuntimeError('native_api_error')
        return obj.get('data', obj) if isinstance(obj, dict) else obj

    def normalize(advanced):
        return {'connections': sorted([{'client': row['client'], 'async': row.get('async', False),
            'sharedDrives': sorted([{'id': x['id'], 'permission': x['permission'],
                'noRootSquash': x.get('noRootSquash', False)} for x in row.get('sharedDrives', [])],
                key=lambda x: x['id'])} for row in advanced['connections']], key=lambda x: x['client'])}

    def read():
        return {'shares': api(SHARES), 'advanced': normalize(api(ADVANCED)),
                'settings': api('/api/v1/services/nfs/settings')}

    with Path('/run/lock/vela-unas.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        before = read()
        rows = [x for x in before['shares'] if x['name'] == 'Unprotected']
        if len(rows) != 1 or not before['settings'].get('enable'):
            raise RuntimeError('expected_existing_share_and_nfs_required')
        share = rows[0]
        wanted = copy.deepcopy(before['advanced'])
        connections = wanted['connections']
        matches = [x for x in connections if x['client'] == CLIENT]
        if len(matches) > 1:
            raise RuntimeError('ambiguous_host_nfs_connection')
        if not matches:
            connections.append({'client': CLIENT, 'async': False, 'sharedDrives': []})
            matches = [connections[-1]]
        host = matches[0]
        if host.get('async'):
            raise RuntimeError('existing_async_connection_not_changed')
        desired = {'id': share['id'], 'permission': 'rw', 'noRootSquash': False}
        existing = [x for x in host['sharedDrives'] if x['id'] == share['id']]
        if existing and any(x.get('permission') != 'rw' or x.get('noRootSquash', False) for x in existing):
            raise RuntimeError('existing_share_export_conflict')
        if not existing:
            host['sharedDrives'].append(desired)
        wanted = normalize(wanted)
        changed = wanted != before['advanced']
        original_hash = digest(before)
        if args.apply:
            if args.expected_sha256 != original_hash:
                raise RuntimeError('configuration_changed_since_read')
            if changed:
                backup = Path('/var/backups/vela-unas')
                backup.mkdir(mode=0o700, exist_ok=True)
                path = backup / (original_hash + '.json')
                if not path.exists():
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, 'w') as stream:
                        json.dump(before, stream)
                if read() != before:
                    raise RuntimeError('configuration_drift')
                api(ADVANCED, 'PUT', wanted)
            after = read()
            if after['shares'] != before['shares'] or after['settings'] != before['settings']:
                raise RuntimeError('other_native_settings_changed')
            if after['advanced'] != wanted:
                raise RuntimeError('nfs_rules_readback_mismatch')
        # Reconcile a preceding apply whose HTTP write succeeded before readback.
        originals = list(Path('/var/backups/vela-unas').glob('*.json')) if Path('/var/backups/vela-unas').exists() else []
        preserved = None
        if originals:
            original = json.loads(originals[0].read_text())
            expected = normalize(original['advanced'])
            host_rule = next(x for x in expected['connections'] if x['client'] == CLIENT)
            if not any(x['id'] == share['id'] for x in host_rule['sharedDrives']):
                host_rule['sharedDrives'].append(desired)
            preserved = (normalize(expected) == read()['advanced']
                and original['shares'] == read()['shares'] and original['settings'] == read()['settings'])
            if not preserved:
                raise RuntimeError('original_configuration_preservation_mismatch')
        print(json.dumps({'ok': True, 'applied': args.apply, 'change_required': changed,
            'before_sha256': original_hash, 'share': 'Unprotected', 'share_id': share['id'],
            'pool': share['storagePoolId'], 'mount_source': '192.168.1.88:/var/nfs/shared/Unprotected',
            'host': CLIENT, 'other_shares_preserved': preserved}))


if __name__ == '__main__':
    main()
