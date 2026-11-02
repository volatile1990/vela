"""Install Vela-owned network rules and the persistent Caddy network attachment."""
import ipaddress
import json
from pathlib import Path
import subprocess


def run(*args, check=True):
    return subprocess.run(args, capture_output=True, text=True, check=check)


def ensure_rule(chain, *rule):
    if run('/usr/sbin/iptables', '-C', chain, *rule, check=False).returncode:
        run('/usr/sbin/iptables', '-I', chain, '1', *rule)


def main():
    wanted = [('vela_proxy', '172.29.50.0/28', 'br-vela-proxy', True),
              ('vela_worker', '172.29.51.0/28', 'br-vela-worker', False)]
    names = run('docker', 'network', 'ls', '--format', '{{.Name}}').stdout.splitlines()
    networks = json.loads(run('docker', 'network', 'inspect', *names).stdout)
    for name, subnet, bridge, internal in wanted:
        existing = next((x for x in networks if x['Name'] == name), None)
        if existing:
            if (existing['Internal'] != internal or existing.get('EnableIPv6')
                or existing['IPAM']['Config'][0]['Subnet'] != subnet
                or existing.get('Options', {}).get('com.docker.network.bridge.name') != bridge):
                raise RuntimeError('existing_network_conflict:' + name)
        else:
            for net in networks:
                for config in net['IPAM'].get('Config') or []:
                    other = config.get('Subnet')
                    if other and ':' not in other and ipaddress.ip_network(other).overlaps(ipaddress.ip_network(subnet)):
                        raise RuntimeError('network_subnet_overlap')
            args = ['docker', 'network', 'create', '--subnet', subnet, '--opt',
                    'com.docker.network.bridge.name=' + bridge]
            if internal:
                args.append('--internal')
            run(*args, name)
    # Commit both exclusively owned chains atomically; preserve all foreign rules.
    rules = ['*filter', ':VELA-EGRESS - [0:0]', ':VELA-HOST - [0:0]',
             '-F VELA-EGRESS', '-F VELA-HOST',
             '-A VELA-EGRESS -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT']
    for cidr in ('0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8',
                 '169.254.0.0/16', '172.16.0.0/12', '192.0.0.0/24', '192.0.2.0/24',
                 '192.168.0.0/16', '198.18.0.0/15', '198.51.100.0/24',
                 '203.0.113.0/24', '224.0.0.0/4', '240.0.0.0/4'):
        rules.append('-A VELA-EGRESS -d ' + cidr + ' -j REJECT')
    rules += ['-A VELA-EGRESS -p tcp -m multiport --dports 80,443 -j ACCEPT',
              '-A VELA-EGRESS -j REJECT',
              '-A VELA-HOST -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT',
              '-A VELA-HOST -j REJECT', 'COMMIT']
    subprocess.run(['/usr/sbin/iptables-restore', '--noflush'], input='\n'.join(rules)+'\n', text=True, check=True, capture_output=True)
    ensure_rule('DOCKER-USER', '-s', '172.29.51.0/28', '-j', 'VELA-EGRESS')
    ensure_rule('INPUT', '-s', '172.29.51.0/28', '-j', 'VELA-HOST')
    compose = Path('/srv/nexus/compose.yaml')
    original = compose.read_text()
    if 'vela_proxy' not in original:
        if original.count('    volumes:\n') != 1 or '\nnetworks:' in original:
            raise RuntimeError('unexpected_caddy_compose_shape')
        updated = original.replace('    volumes:\n', '    networks:\n      - default\n      - vela_proxy\n    volumes:\n', 1)
        updated += '\nnetworks:\n  vela_proxy:\n    external: true\n    name: vela_proxy\n'
        backup = Path('/srv/vela/secrets/nexus-compose-before.yaml')
        if not backup.exists():
            backup.write_text(original)
            backup.chmod(0o600)
        if compose.read_text() != original:
            raise RuntimeError('caddy_compose_drift')
        compose.write_text(updated)
        try:
            run('docker', 'compose', '-f', str(compose), 'config', '--quiet')
        except Exception:
            if compose.read_text() == updated:
                compose.write_text(original)
            raise
    proxy = json.loads(run('docker', 'inspect', 'nexus-proxy').stdout)[0]
    if 'vela_proxy' not in proxy['NetworkSettings']['Networks']:
        run('docker', 'network', 'connect', '--ip', '172.29.50.2', 'vela_proxy', 'nexus-proxy')
    print(json.dumps({'ok': True, 'networks': [x[0] for x in wanted], 'worker_private_egress_blocked': True,
                      'proxy_restarted': False}))


if __name__ == '__main__':
    main()
