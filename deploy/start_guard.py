"""Run before either service; never start against unmounted host directories."""
import os
from pathlib import Path
import subprocess
import sys

if not os.path.ismount('/srv/vela/library') or not os.path.ismount('/srv/vela/work'):
    raise SystemExit('Vela storage mount is unavailable')
result = subprocess.run(['findmnt', '-n', '-o', 'SOURCE,FSTYPE', '--mountpoint', '/srv/vela/library'],
    capture_output=True, text=True, check=True).stdout.split()
if result != ['192.168.1.88:/var/nfs/shared/Unprotected', 'nfs']:
    raise SystemExit('Vela NAS mount identity mismatch')
if Path('/srv/vela/library/Vela').is_symlink():
    raise SystemExit('Vela download folder must not be a symlink')
expected = 'vela-unas-unprotected-698f83b4-014e-482f-8aa1-d5a75e65b39a'
marker = Path('/srv/vela/library/Vela/.vela-volume')
if marker.is_symlink() or marker.read_text().strip() != expected:
    raise SystemExit('Vela download volume identity mismatch')
subprocess.run(['/usr/sbin/iptables', '-C', 'DOCKER-USER', '-s', '172.29.51.0/28', '-j', 'VELA-EGRESS'], check=True)
rules = subprocess.run(['/usr/sbin/iptables', '-S', 'VELA-EGRESS'], capture_output=True,
    text=True, check=True).stdout.splitlines()
denied = ('0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8',
          '169.254.0.0/16', '172.16.0.0/12', '192.0.0.0/24', '192.0.2.0/24',
          '192.168.0.0/16', '198.18.0.0/15', '198.51.100.0/24',
          '203.0.113.0/24', '224.0.0.0/4', '240.0.0.0/4')
if len(rules) != len(denied) + 4 or '--ctstate' not in rules[1]:
    raise SystemExit('Vela egress chain differs from the required rules')
for index, cidr in enumerate(denied, 2):
    if ('-d ' + cidr + ' -j REJECT') not in rules[index]:
        raise SystemExit('Vela private network deny rule is unavailable')
if '--dports 80,443 -j ACCEPT' not in rules[-2] or not rules[-1].startswith('-A VELA-EGRESS -j REJECT'):
    raise SystemExit('Vela public network allow rule differs')
subprocess.run(['/usr/sbin/iptables', '-C', 'INPUT', '-s', '172.29.51.0/28', '-j', 'VELA-HOST'], check=True)
subprocess.run(['/usr/sbin/iptables', '-C', 'VELA-HOST', '-j', 'REJECT'], check=True)
