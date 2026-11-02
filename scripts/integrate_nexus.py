"""Add only Vela's app tile to the existing private NEXUS board."""
import copy
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path.home() / '.codex/tools/home-infrastructure'))
import homarr_access

api = homarr_access.client()
before = api.board('NEXUS')
apps = api.trpc('app.selectable')
found = [x for x in apps if x.get('name') == 'Vela']
if len(found) > 1:
    raise RuntimeError('ambiguous_vela_app')
if found:
    app = found[0]
    if app.get('href') != 'https://vela.home.arpa/':
        raise RuntimeError('existing_vela_destination_conflict')
else:
    if api.board('NEXUS') != before:
        raise RuntimeError('board_changed_during_read')
    icon = Path(__file__).resolve().parents[1] / 'frontend/public/favicon.svg'
    media = api.trpc('media.getPaginated', {'page': 1, 'pageSize': 100,
        'includeFromAllUsers': False, 'search': 'vela.svg'})
    matches = [x for x in media['items'] if x['name'] == 'vela.svg']
    if len(matches) != 1:
        raise RuntimeError('expected_single_uploaded_vela_icon')
    icon_url = '/api/user-medias/' + matches[0]['id']
    with api._opener.open(api.base_url + icon_url, timeout=15) as response:
        content = response.read(4097)
        if response.headers.get_content_type() != 'image/svg+xml' or len(content) > 4096 or b'<svg' not in content or b'<script' in content or b'href=' in content:
            raise RuntimeError('existing_vela_icon_mismatch')
    app = api.create_app(name='Vela', href='https://vela.home.arpa/', icon_url=icon_url,
        description='Video und Audio herunterladen.', ping_url=None)
items = [x for x in before['items'] if x.get('options', {}).get('appId') == app['id']]
if len(items) > 1:
    raise RuntimeError('ambiguous_vela_tile')
if not items:
    api.add_item(before['id'], 'app', {'appId': app['id']}, width=6, height=1)
after = api.board('NEXUS')
others = [x for x in after['items'] if x.get('options', {}).get('appId') != app['id']]
previous = [x for x in before['items'] if x.get('options', {}).get('appId') != app['id']]
if others != previous:
    raise RuntimeError('existing_board_items_changed')
after_apps = api.trpc('app.selectable')
if [x for x in after_apps if x['id'] != app['id']] != [x for x in apps if x['id'] != app['id']]:
    raise RuntimeError('other_app_definitions_changed')
print(json.dumps({'ok': True, 'app': 'Vela', 'url': app['href'],
    'tile_count': len([x for x in after['items'] if x.get('options', {}).get('appId') == app['id']]),
    'other_apps_and_items_preserved': True}))
