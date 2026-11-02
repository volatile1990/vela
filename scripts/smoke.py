"""Check the LAN-only deployed application, including real media publication."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import time
import urllib.error
import urllib.request
import uuid

URL = 'https://vela.home.arpa'
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(path, body=None):
    req = urllib.request.Request(URL + path, data=None if body is None else json.dumps(body).encode(),
        headers={'Origin': URL, 'Content-Type': 'application/json'})
    with OPENER.open(req, timeout=20) as response:
        return json.load(response)


def wait(job, seconds=120):
    until = time.monotonic() + seconds
    previous = None
    while time.monotonic() < until:
        job = request('/api/jobs/' + job['id'])
        if job['status'] != previous:
            print(json.dumps({'job': job['id'], 'kind': job['kind'], 'status': job['status']}), flush=True)
            previous = job['status']
        if job['status'] in {'completed', 'failed', 'cancelled'}:
            return job
        time.sleep(1)
    raise RuntimeError('Smoke job timed out: ' + job['id'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4')
    parser.add_argument('--profile', default='original', choices=['original', 'audio', 'compatible'])
    parser.add_argument('--preview-only', action='store_true')
    args = parser.parse_args()
    for attempt in range(30):
        try:
            overview = request('/api/overview')
            if overview.get('storage', {}).get('available'):
                break
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1)
    else:
        raise RuntimeError('Vela startup did not become ready')
    assert overview['version'] == '1.0.0' and overview['storage']['available']
    body = {'url': args.url, 'profile': args.profile, 'max_height': 720, 'playlist': False}
    preview = wait(request('/api/previews', body))
    report = {'version': overview['version'], 'preview': {'id': preview['id'], 'status': preview['status'],
        'error': preview.get('error'), 'title': preview.get('title')}}
    if preview['status'] != 'completed':
        print(json.dumps(report, ensure_ascii=False));raise SystemExit(1)
    if not args.preview_only:
        job = wait(request('/api/jobs', body))
        report['download'] = {'id': job['id'], 'status': job['status'], 'profile': args.profile,
            'error': job.get('error'), 'artifacts': []}
        if job['status'] != 'completed' or not job.get('artifacts'):
            print(json.dumps(report, ensure_ascii=False));raise SystemExit(1)
        for artifact in job['artifacts']:
            digest = hashlib.sha256(); size = 0
            with OPENER.open(URL + '/api/artifacts/' + artifact['id'] + '/download', timeout=30) as response:
                while chunk := response.read(1024*1024):
                    digest.update(chunk);size += len(chunk)
            assert digest.hexdigest() == artifact['sha256'] and size == artifact['size']
            report['download']['artifacts'].append({'relative_path': artifact['relative_path'],
                'size': size, 'sha256': digest.hexdigest()})
    out = Path(__file__).resolve().parents[1] / 'output'
    out.mkdir(exist_ok=True)
    (out / ('smoke-' + args.profile + '-' + str(uuid.uuid4()) + '.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
