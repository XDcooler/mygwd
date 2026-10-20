#!/usr/bin/env python3
"""Test an installed gateway with its real TLS node, without substituting a node."""
import argparse
import http.cookiejar
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import ssl
import subprocess
import sys
import time
import urllib.request

from test_gateway import (Suite, NoRedirect, command, probe_dns, run_backend, LAN_IP, GW_LAN,
                          BACKEND, GW_BACKEND, TARGETS)


class RealSuite(Suite):
    def check(self, name, action):
        if name != 'direct_internet_baseline' or not self.args.shared_upstream_gateway:
            return super().check(name, action)
        started = time.monotonic()
        try:
            evidence = action()
            state = 'limited' if evidence.pop('environment_limit', False) else 'passed'
            row = {'name': name, 'status': state, 'evidence': evidence}
        except Exception as error:
            row = {'name': name, 'status': 'failed', 'error': str(error)}
        row['seconds'] = round(time.monotonic() - started, 3)
        self.results.append(row)
        print(row['status'].upper() + ' ' + name, flush=True)
        self.report.joinpath('results.json').write_text(json.dumps(self.results, indent=2) + '\n')
        return row['status'] != 'failed'

    def preflight(self):
        assert os.geteuid() == 0
        for unit in ('vtrui', 'iptables-proxy', 'pihole-FTL', 'nginx', 'php8.4-fpm'):
            command(['systemctl', 'is-active', '--quiet', unit])
        config = json.loads(self.root.joinpath('vtrui/config.json').read_text())
        outbound = config['outbounds'][0]
        assert outbound['protocol'] == 'vless'
        assert outbound['streamSettings']['security'] == 'tls'
        assert outbound['streamSettings']['tlsSettings']['allowInsecure'] is False
        assert outbound['settings']['vnext'][0]['address'] != BACKEND
        for line in self.root.joinpath('chnrouteSET').read_text().splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[:2] == ['add', 'chnroute']:
                network = ipaddress.ip_network(parts[2])
                if network.is_global and network.num_addresses >= 32:
                    TARGETS['china'] = str(network.network_address + 10)
                    TARGETS['force_proxy'] = str(network.network_address + 20)
                    break
        else:
            raise RuntimeError('No current China network can support the direct-route fixture')
        self.foreign_ip = socket.gethostbyname('www.cloudflare.com')
        self.direct_exit = None
        self.direct_udp_exit = None
        self.context = ssl.create_default_context(cafile='/var/www/ssl/de_GWD.cer')
        self.web = self.opener(self.web_cookies)
        with sqlite3.connect('/etc/pihole/gravity.db') as database:
            count = database.execute('select count(*) from gravity where domain=?', ('doubleclick.net',)).fetchone()[0]
        assert count > 0, 'Required ad domain is absent from real gravity data'
        return {'node': outbound['settings']['vnext'][0]['address'], 'tls_verification': True,
                'gravity_entries_for_ad': count, 'no_local_vless_backend': True}

    def opener(self, cookies=None):
        handlers = [urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=self.context), NoRedirect()]
        if cookies is not None:
            handlers.append(urllib.request.HTTPCookieProcessor(cookies))
        return urllib.request.build_opener(*handlers)

    def web_request(self, path, data=None, opener=None):
        return super().web_request(path, data, self.opener() if opener is not None else self.web)

    def setup(self):
        for name in (self.lan, self.backend):
            command(['ip', 'netns', 'add', name])
            self.started_namespaces.append(name)
            self.ns(name, ['ip', 'link', 'set', 'lo', 'up'])
            resolver = Path('/etc/netns')/name
            resolver.mkdir(parents=True, exist_ok=False)
            self.resolver_directories.append(resolver)
            resolver.joinpath('resolv.conf').write_text('nameserver ' + GW_LAN + '\n')
        for name, interface, gateway, peer in ((self.lan, self.lan_if, GW_LAN, LAN_IP),
                                               (self.backend, self.up_if, GW_BACKEND, BACKEND)):
            command(['ip', 'link', 'add', interface, 'type', 'veth', 'peer', 'name', 'peer0', 'netns', name])
            command(['ip', 'addr', 'add', gateway + '/24', 'dev', interface])
            command(['ip', 'link', 'set', interface, 'up'])
            self.ns(name, ['ip', 'addr', 'add', peer + '/24', 'dev', 'peer0'])
            self.ns(name, ['ip', 'link', 'set', 'peer0', 'up'])
            command(['sysctl', '-q', '-w', f'net.ipv4.conf.{interface}.rp_filter=0'])
        self.ns(self.lan, ['ip', 'route', 'add', 'default', 'via', GW_LAN])
        self.ns(self.backend, ['ip', 'route', 'add', '192.168.231.0/24', 'via', GW_BACKEND])
        for address in TARGETS.values():
            self.ns(self.backend, ['ip', 'addr', 'add', address + '/32', 'dev', 'lo'])
        for network in ('203.0.113.0/24', TARGETS['china'] + '/32',
                        TARGETS['force_proxy'] + '/32', '10.231.0.0/24'):
            command(['ip', 'route', 'add', network, 'via', BACKEND, 'dev', self.up_if])
        self.old_forward = Path('/proc/sys/net/ipv4/ip_forward').read_text().strip()
        command(['sysctl', '-q', '-w', 'net.ipv4.ip_forward=1'])
        interface = command(['ip', '-o', 'route', 'show', 'default']).stdout.split(' dev ', 1)[1].split()[0]
        nat = ['iptables', '-t', 'nat', '-A', 'POSTROUTING', '-s', LAN_IP + '/32', '-o', interface,
               '-m', 'comment', '--comment', self.tag, '-j', 'MASQUERADE']
        command(nat)
        self.sentinel_rules.append(nat)
        # The auxiliary backend only measures China/private direct routes.
        # It has no default route and runs HTTP/UDP/DNS, never a VLESS server.
        self.processes.append(subprocess.Popen(['ip', 'netns', 'exec', self.backend, 'python3',
            str(Path(__file__).resolve()), 'backend', str(self.work/'backend.jsonl'), json.dumps(TARGETS)],
            stdout=open(self.report/'direct-backend.log', 'w'), stderr=subprocess.STDOUT))
        self.write('/etc/dnsmasq.d/99-de-gwd-real-integration.conf', 'interface=' + self.lan_if + '\n')
        self.touched_services = True
        command(['systemctl', 'restart', 'pihole-FTL'])
        time.sleep(0.3)
        command(['ipset', 'test', 'chnroute', TARGETS['china']])
        return {'lan': LAN_IP, 'node_configuration_unchanged': True, 'real_proxy_node': True}

    def real_https(self, expected=None):
        result = self.ns(self.lan, ['env', '-i', 'PATH=/usr/bin:/bin', 'curl', '-q', '-4', '--noproxy', '*',
            '-fsS', '--connect-timeout', '8', '--max-time', '20', '--resolve',
            'www.cloudflare.com:443:' + self.foreign_ip, 'https://www.cloudflare.com/cdn-cgi/trace'])
        trace = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        ipaddress.ip_address(trace['ip'])
        if expected:
            assert trace['ip'] == expected, f'Exit IP {trace["ip"]} != {expected}'
        return {'exit_ip': trace['ip'], 'proxy_environment_cleared': True, 'tls_verification': True}

    def real_udp(self, expected=None):
        result = self.ns(self.lan, ['env', '-i', 'PATH=/usr/bin:/bin', 'dig', '-4', '@1.1.1.1',
            'whoami.cloudflare', 'TXT', 'CH', '+short', '+time=5', '+tries=1'], timeout=8)
        address = result.stdout.strip().strip('"')
        ipaddress.ip_address(address)
        if expected:
            assert address == expected, f'UDP exit IP {address} != {expected}'
        return {'udp_dns_remote_observed_ip': address, 'dns_transport': 'udp', 'remote': '1.1.1.1:53'}

    def normal_dns(self, tcp=False):
        answer = self.dns(GW_LAN, 'example.com', tcp)
        assert answer['rcode'] == 0 and answer['addresses'] and '0.0.0.0' not in answer['addresses'], str(answer)
        return answer

    def login(self):
        assert self.web_request('act/uptime.php', opener=self.opener())[0] == 403
        assert self.web_request('act/onUDP.php', opener=self.opener())[0] == 403
        self.web_request('auth.php', {'gwdpw': 'deliberately-wrong-integration-password'})
        assert self.web_request('act/uptime.php')[0] == 403
        access = json.loads(Path(self.args.access_file).read_text())
        status, _, headers = self.web_request('auth.php', {'gwdpw': access['password']})
        del access
        assert status == 200
        cookies = headers.get_all('Set-Cookie', [])
        assert any('persistentlogin=' in value and 'HttpOnly' in value and 'SameSite=Strict' in value
                   and 'secure' in value.lower() for value in cookies)
        assert self.web_request('act/uptime.php')[0] == 200
        return {'anonymous_read_and_write': 403, 'wrong_password_denied': True,
                'authenticated_action': 200, 'secure_cookie': True, 'certificate_verified': True}

    def toggle_ui(self, enabled):
        self.save(self.root/'0conf')
        self.save(self.root/'iptables-proxy-up')
        assert self.web_request('act/onUDP.php' if enabled else 'act/offUDP.php')[0] == 200
        for _ in range(100):
            actual = command([self.root/'ui-checkUDP']).stdout.strip()
            if actual == ('success' if enabled else 'outline-secondary'):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError('Web UDP action did not change the actual runtime')
        self.ui_indicator(enabled)
        return {'udp_enabled': enabled, 'https': self.real_https(self.args.expected_exit),
                'udp': self.real_udp(self.args.expected_exit if enabled else self.direct_udp_exit)}

    def toggle_cli(self, enabled):
        self.udp_mode(enabled)
        return {'udp_enabled': enabled, 'https': self.real_https(self.args.expected_exit),
                'udp': self.real_udp(self.args.expected_exit if enabled else self.direct_udp_exit)}

    def direct_baseline(self):
        evidence = self.real_https()
        self.direct_exit = evidence['exit_ip']
        udp = self.real_udp()
        self.direct_udp_exit = udp['udp_dns_remote_observed_ip']
        if (self.direct_exit == self.args.expected_exit or self.direct_udp_exit == self.args.expected_exit) and self.args.shared_upstream_gateway:
            address = ipaddress.ip_address(self.args.shared_upstream_gateway)
            assert address.version == 4 and address.is_private
            return {'tcp': evidence, 'udp': udp, 'environment_limit': True,
                    'upstream_gateway': str(address), 'distinct_exit_available': False,
                    'limitation': 'Host gateway already uses this proxy exit; exit IP cannot prove bypass.'}
        assert self.direct_exit != self.args.expected_exit, 'Direct and proxy exits cannot be distinguished'
        assert self.direct_udp_exit != self.args.expected_exit, 'Direct and proxy UDP exits cannot be distinguished'
        return {'tcp': evidence, 'udp': udp}

    def restart_real_services(self):
        for service in ('vtrui', 'pihole-FTL', 'nginx', 'php8.4-fpm', 'iptables-proxy'):
            command(['systemctl', 'restart', service])
            command(['systemctl', 'is-active', '--quiet', service])
        self.sentinels()
        return {'tcp': self.real_https(self.args.expected_exit), 'udp': self.real_udp(self.args.expected_exit),
                'dns': self.normal_dns(), 'ad': self.ad_dns()}

    def gravity_log(self):
        for path in (Path('/var/log/pihole/pihole.log'), Path('/var/log/pihole.log')):
            if path.exists():
                lines = [line for line in path.read_text().splitlines() if 'doubleclick.net' in line]
                if any(LAN_IP in line for line in lines) and any('gravity' in line for line in lines):
                    self.report.joinpath('pihole-gravity-evidence.log').write_text('\n'.join(lines) + '\n')
                    return {'lan_client': LAN_IP, 'gravity_block_confirmed': True}
        raise RuntimeError('LAN query and gravity block are absent from Pi-hole logs')

    def run(self):
        try:
            if not self.check('real_node_and_services', self.preflight): return 1
            if not self.check('lan_and_direct_route_fixtures', self.setup): return 1
            self.check('external_firewall_sentinels', self.setup_sentinels)
            self.check('stop_preserves_firewall', lambda: self.toggle_proxy(False))
            self.check('direct_internet_baseline', self.direct_baseline)
            self.check('start_preserves_firewall', lambda: self.toggle_proxy(True))
            self.check('real_node_transparent_tcp', lambda: self.real_https(self.args.expected_exit))
            self.check('china_tcp_direct', lambda: self.http(TARGETS['china'], LAN_IP))
            self.check('private_tcp_direct', lambda: self.http(TARGETS['private'], LAN_IP))
            if self.args.access_file:
                self.check('real_web_authentication', self.login)
                self.check('real_web_pages', self.ui_pages)
                self.check('web_enable_udp_and_real_traffic', lambda: self.toggle_ui(True))
            else:
                self.check('enable_udp_and_real_traffic', lambda: self.toggle_cli(True))
            self.check('real_node_transparent_udp', lambda: self.real_udp(self.args.expected_exit))
            self.check('china_udp_direct', lambda: self.udp(TARGETS['china'], LAN_IP))
            self.check('private_udp_direct', lambda: self.udp(TARGETS['private'], LAN_IP))
            self.check('normal_udp_dns', self.normal_dns)
            self.check('normal_tcp_dns', lambda: self.normal_dns(True))
            self.check('ad_udp_dns_blocked', self.ad_dns)
            self.check('ad_tcp_dns_blocked', lambda: self.ad_dns(True))
            self.check('gravity_log_evidence', self.gravity_log)
            toggle = self.toggle_ui if self.args.access_file else self.toggle_cli
            self.check('disable_udp_and_real_traffic', lambda: toggle(False))
            self.check('reenable_udp_and_real_traffic', lambda: toggle(True))
            self.check('service_restart_and_functional_recheck', self.restart_real_services)
            if self.args.access_file:
                self.check('web_logout', self.ui_logout)
            self.check('live_kernel_snapshot', lambda: self.snapshot('real-node'))
        finally:
            self.check('cleanup', self.cleanup)
            self.finish_report()
        return int(any(row['status'] == 'failed' for row in self.results))

    def finish_report(self):
        passed = sum(row['status'] == 'passed' for row in self.results)
        limited = sum(row['status'] == 'limited' for row in self.results)
        failed = sum(row['status'] == 'failed' for row in self.results)
        lines = ['# Real-node Debian 13 gateway integration', '',
                 f'{passed} passed; {failed} failed; {limited} limited by the test environment.', '',
                 'The installed real VLESS+WS+TLS node is preserved throughout.',
                 'Internet TCP and UDP exit IPs are checked against the real server.',
                 'Isolated HTTP/UDP fixtures only test China/private direct routes; no local VLESS server runs.',
                 '', '| Check | Result |', '| --- | --- |']
        lines += [f'| {row["name"]} | {row["status"]} |' for row in self.results]
        self.report.joinpath('report.md').write_text('\n'.join(lines) + '\n')


def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'backend':
        TARGETS.update(json.loads(sys.argv[3]))
        run_backend(sys.argv[2])
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == 'dns':
        print(json.dumps(probe_dns(sys.argv[2], sys.argv[3], sys.argv[4] == 'tcp')))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='/opt/de_GWD')
    parser.add_argument('--report-dir', required=True)
    parser.add_argument('--expected-exit', required=True)
    parser.add_argument('--shared-upstream-gateway', help='Explicitly record a private host gateway already using the same proxy exit as an environment limitation, never as a passing bypass test')
    parser.add_argument('--access-file', help='Optional local JSON holding the independently authorized UI test password')
    parser.add_argument('--web-base-url', default='https://localhost')
    args = parser.parse_args()
    args.node_port = 0
    args.web_password_file = None
    return RealSuite(args).run()


if __name__ == '__main__':
    sys.exit(main())
