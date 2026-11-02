"""Install Vela-only systemd roles; keep the two containers independently restartable."""
import os
from pathlib import Path
import subprocess

BASE = Path('/etc/systemd/system')


def put(name, content, *, previous=None):
    target = BASE / name
    if target.exists() and target.read_text() != content:
        if previous is None or target.read_text() != previous:
            raise RuntimeError('existing_unit_conflict:' + name)
        target.write_text(content)
    if not target.exists():
        target.write_text(content)


if os.getuid() != 0:
    raise SystemExit('root required')
put('vela-network.service', '''[Unit]
Description=Vela isolated Docker networks and egress rules
Requires=docker.service
PartOf=docker.service
After=docker.service
Before=vela-api.service vela-worker.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/python3 /opt/vela/current/deploy/network.py
[Install]
WantedBy=multi-user.target
''')
for role in ('api', 'worker', 'browser'):
    put('vela-' + role + '.service', f'''[Unit]
Description=Vela {role}
Requires=docker.service vela-network.service srv-vela-work.mount srv-vela-library.mount
PartOf=docker.service
After=docker.service vela-network.service srv-vela-work.mount srv-vela-library.mount
BindsTo=srv-vela-work.mount srv-vela-library.mount
StartLimitIntervalSec=120
StartLimitBurst=10
[Service]
Type=simple
WorkingDirectory=/opt/vela/current
ExecStartPre=/usr/bin/python3 /opt/vela/current/deploy/start_guard.py {role}
ExecStart=/usr/bin/docker compose -p vela -f /opt/vela/current/deploy/compose.yaml up --no-build --pull never --no-deps --exit-code-from {role} {role}
ExecStop=/usr/bin/docker compose -p vela -f /opt/vela/current/deploy/compose.yaml stop -t 25 {role}
Restart=on-failure
RestartSec=5
TimeoutStopSec=45
[Install]
WantedBy=multi-user.target
''')
old_target = '''[Unit]
Description=Vela media downloader
Wants=vela-api.service vela-worker.service
After=vela-api.service vela-worker.service
[Install]
WantedBy=multi-user.target
'''
put('vela.target', old_target.replace('vela-api.service vela-worker.service', 'vela-api.service vela-worker.service vela-browser.service'), previous=old_target)
subprocess.run(['systemctl', 'daemon-reload'], check=True)
subprocess.run(['systemctl', 'enable', 'vela-network.service', 'vela-api.service', 'vela-worker.service', 'vela-browser.service', 'vela.target'], check=True)
print('Vela service units installed')
