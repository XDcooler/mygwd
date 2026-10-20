#!/usr/bin/env python3
"""Real Debian 11 LAN/TPROXY/Pi-hole checks against an isolated local backend.

Run in the prepared Debian 11 guest as root, after deploying the client with a
controlled VLESS node at 198.18.231.2:14443. No Internet destination is used.
The result records source addresses, request nonces, service transitions and
firewall preservation; it never includes the node UUID or client configuration.
"""
import argparse
import contextlib
import datetime
import http.server
import http.cookiejar
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import socketserver
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import urllib.error
import urllib.parse
import urllib.request

LAN_IP = '192.168.231.2'
GW_LAN = '192.168.231.1'
BACKEND = '198.18.231.2'
GW_BACKEND = '198.18.231.1'
TARGETS = {'foreign': '203.0.113.10', 'china': '1.0.1.10',
           'private': '10.231.0.10', 'force_direct': '203.0.113.20',
           'force_proxy': '1.0.1.20'}
HTTP_PORT = 18080
UDP_PORT = 18081
DNS_GOOD = 'gateway-allowed.test'
DNS_AD = 'doubleclick.net'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def command(argv, *, check=True, timeout=30, input=None):
    result = subprocess.run(list(map(str, argv)), input=input, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f'{argv[0]} exited {result.returncode}: {result.stderr.strip()}')
    return result


def encode_dns(name, identifier):
    return struct.pack('!6H', identifier, 0x100, 1, 0, 0, 0) + b''.join(
        bytes([len(label)]) + label.encode('ascii') for label in name.split('.')) + b'\0' + struct.pack('!HH', 1, 1)


def skip_name(packet, pos):
    while packet[pos]:
        if packet[pos] & 0xc0 == 0xc0:
            return pos + 2
        pos += packet[pos] + 1
    return pos + 1


def decode_dns(packet, identifier):
    answer_id, flags, questions, answers, _, _ = struct.unpack('!6H', packet[:12])
    if answer_id != identifier:
        raise ValueError('DNS response ID differs from the request')
    pos = 12
    for _ in range(questions):
        pos = skip_name(packet, pos) + 4
    addresses = []
    for _ in range(answers):
        pos = skip_name(packet, pos)
        kind, cls, ttl, length = struct.unpack('!HHIH', packet[pos:pos + 10])
        pos += 10
        if kind == 1 and cls == 1 and length == 4:
            addresses.append(socket.inet_ntoa(packet[pos:pos + length]))
        pos += length
    return {'rcode': flags & 15, 'addresses': addresses}


def recv_exact(sock, length):
    data = b''
    while len(data) < length:
        part = sock.recv(length - len(data))
        if not part:
            raise ValueError('TCP DNS response ended early')
        data += part
    return data


def probe_dns(address, name, tcp):
    identifier = int.from_bytes(os.urandom(2), 'big')
    packet = encode_dns(name, identifier)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM) as sock:
        sock.settimeout(5)
        if tcp:
            sock.connect((address, 53))
            sock.sendall(struct.pack('!H', len(packet)) + packet)
            response = recv_exact(sock, struct.unpack('!H', recv_exact(sock, 2))[0])
        else:
            sock.sendto(packet, (address, 53))
            response, peer = sock.recvfrom(4096)
            if peer != (address, 53):
                raise ValueError(f'Unexpected DNS responder: {peer}')
    answer = decode_dns(response, identifier)
    answer['query_id'] = identifier
    return answer


def run_backend(log_file):
    lock = threading.Lock()

    def record(entry):
        with lock, open(log_file, 'a') as stream:
            stream.write(json.dumps(entry, sort_keys=True) + '\n')

    class HTTP(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            entry = {'protocol': 'tcp', 'peer': self.client_address[0],
                     'local': self.connection.getsockname()[0], 'nonce': self.path[1:]}
            record(entry)
            body = json.dumps(entry).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    class Echo(socketserver.BaseRequestHandler):
        def handle(self):
            data, sock = self.request
            entry = {'protocol': 'udp', 'peer': self.client_address[0], 'local': sock.getsockname()[0],
                     'nonce': data.decode('ascii')}
            record(entry)
            sock.sendto(json.dumps(entry).encode(), self.client_address)

    def dns_answer(packet, peer, transport):
        end = skip_name(packet, 12) + 4
        record({'protocol': 'dns', 'transport': transport, 'peer': peer,
                'query_id': struct.unpack('!H', packet[:2])[0], 'question': packet[12:end].hex()})
        qtype = struct.unpack('!H', packet[end - 4:end - 2])[0]
        if qtype != 1:
            return packet[:2] + struct.pack('!5H', 0x8180, 1, 0, 0, 0) + packet[12:end]
        return packet[:2] + struct.pack('!5H', 0x8180, 1, 1, 0, 0) + packet[12:end] + \
            b'\xc0\x0c' + struct.pack('!HHIH', 1, 1, 0, 4) + socket.inet_aton(TARGETS['foreign'])

    class DNSUDP(socketserver.BaseRequestHandler):
        def handle(self):
            data, sock = self.request
            sock.sendto(dns_answer(data, self.client_address[0], 'udp'), self.client_address)

    class DNSTCP(socketserver.BaseRequestHandler):
        def handle(self):
            length = struct.unpack('!H', recv_exact(self.request, 2))[0]
            answer = dns_answer(recv_exact(self.request, length), self.client_address[0], 'tcp')
            self.request.sendall(struct.pack('!H', len(answer)) + answer)

    servers = [http.server.ThreadingHTTPServer(('0.0.0.0', HTTP_PORT), HTTP),
               socketserver.ThreadingTCPServer(('0.0.0.0', 53), DNSTCP)]
    for address in TARGETS.values():
        # Bind each destination explicitly so a direct UDP reply keeps the
        # original target as its source rather than the veth's source address.
        servers.append(socketserver.ThreadingUDPServer((address, UDP_PORT), Echo))
    for address in (BACKEND, TARGETS['foreign']):
        servers.append(socketserver.ThreadingUDPServer((address, 53), DNSUDP))
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Event().wait()


class Suite:
    def __init__(self, args):
        self.args = args
        self.root = Path(args.root)
        self.report = Path(args.report_dir)
        self.report.mkdir(parents=True, exist_ok=False)
        self.work = Path(tempfile.mkdtemp(prefix='de-gwd-integration-', dir='/run'))
        self.tag = 'gwd-it-' + uuid.uuid4().hex[:6]
        self.lan = self.tag + '-lan'
        self.backend = self.tag + '-up'
        self.lan_if = 'itlan' + self.tag[-6:]
        self.up_if = 'itup' + self.tag[-6:]
        self.results = []
        self.backups = {}
        self.processes = []
        self.old_forward = None
        self.calibration = {}
        self.sentinel_rules = []
        self.started_namespaces = []
        self.route_specs = []
        self.marker_rule = False
        self.marker_route = False
        self.touched_services = False
        self.backend_server = None
        self.backend_server_argv = None
        self.pihole_log_start = 0
        self.resolver_directories = []
        self.web_cookies = http.cookiejar.CookieJar()
        self.web = urllib.request.build_opener(urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self.web_cookies), NoRedirect())

    def save(self, path):
        path = Path(path)
        if path not in self.backups:
            self.backups[path] = (path.read_bytes(), path.stat().st_mode & 0o777) if path.exists() else None

    def write(self, path, content):
        self.save(path)
        Path(path).write_text(content)
        if Path(path).parent == Path('/etc/dnsmasq.d'):
            Path(path).chmod(0o644)  # Pi-hole 6 reads snippets as its unprivileged service user.

    def ns(self, name, argv, **kwargs):
        return command(['ip', 'netns', 'exec', name, *argv], **kwargs)

    def check(self, name, action):
        started = time.monotonic()
        try:
            evidence = action() or {}
            row = {'name': name, 'status': 'passed', 'evidence': evidence}
        except Exception as exc:
            row = {'name': name, 'status': 'failed', 'error': str(exc)}
            print(f'FAIL {name}: {exc}', flush=True)
        row['seconds'] = round(time.monotonic() - started, 3)
        self.results.append(row)
        if row['status'] == 'passed':
            print(f'PASS {name}', flush=True)
        self.report.joinpath('results.json').write_text(json.dumps(self.results, indent=2) + '\n')
        return row['status'] == 'passed'

    def http(self, address, expected, namespace=None, expected_local=None):
        nonce = uuid.uuid4().hex
        namespace = namespace or self.lan
        # env -i plus --noproxy makes the absence of client-side proxy explicit.
        response = self.ns(namespace, ['env', '-i', 'PATH=/usr/bin:/bin',
            'curl', '-q', '-4', '--noproxy', '*', '-fsS', '--max-time', '8',
            f'http://{address}:{HTTP_PORT}/{nonce}'], timeout=12)
        entry = json.loads(response.stdout)
        assert entry['peer'] == expected, f"peer {entry['peer']} != expected {expected}"
        expected_local = expected_local or address
        assert entry['local'] == expected_local, f"target {entry['local']} != expected {expected_local}"
        assert entry['nonce'] == nonce, 'response nonce mismatch'
        self.verify_backend(entry)
        return entry

    def udp(self, address, expected):
        nonce = uuid.uuid4().hex
        response = self.ns(self.lan, ['env', '-i', 'PATH=/usr/bin:/bin', 'python3',
            str(Path(__file__).resolve()), 'udp', address, nonce], timeout=10)
        entry = json.loads(response.stdout)
        assert entry['peer'] == expected, f"peer {entry['peer']} != expected {expected}"
        assert entry['local'] == address, f"target {entry['local']} != requested {address}"
        assert entry['nonce'] == nonce, 'response nonce mismatch'
        self.verify_backend(entry)
        return entry

    def verify_backend(self, entry):
        records = [json.loads(line) for line in self.work.joinpath('backend.jsonl').read_text().splitlines()]
        assert entry in records, 'backend did not record the observed response'

    def dns(self, address, name, tcp=False):
        response = self.ns(self.lan, ['env', '-i', 'PATH=/usr/bin:/bin', 'python3',
            str(Path(__file__).resolve()), 'dns', address, name, 'tcp' if tcp else 'udp'], timeout=10)
        return json.loads(response.stdout)

    def preflight(self):
        assert os.geteuid() == 0, 'Run this integration suite as root in the disposable Debian guest'
        assert Path('/etc/debian_version').read_text().strip().startswith('11.'), 'Debian 11 required'
        for name in ('ip', 'iptables', 'ipset', 'systemctl', 'curl', 'jq', 'ss'):
            assert shutil.which(name), f'Missing executable: {name}'
        for name in ('vtrui', 'iptables-proxy', 'pihole-FTL'):
            command(['systemctl', 'is-active', '--quiet', name])
        config = json.loads(self.root.joinpath('vtrui/config.json').read_text())
        node = config['outbounds'][0]['settings']['vnext'][0]
        assert node['address'] == BACKEND and node['port'] == self.args.node_port, \
            'Client must point to the controlled local VLESS node'
        assert config['outbounds'][0]['protocol'] == 'vless', 'Local fixture requires VLESS'
        self.node_id = node['users'][0]['id']
        with sqlite3.connect('/etc/pihole/gravity.db') as db:
            assert db.execute('select count(*) from gravity where domain=?', (DNS_AD,)).fetchone()[0] > 0, \
                f'{DNS_AD} is missing from the real gravity database'
            adlists = db.execute('select distinct a.address, a.enabled from gravity g '
                                 'join adlist a on a.id=g.adlist_id where g.domain=?', (DNS_AD,)).fetchall()
            assert any(enabled for address, enabled in adlists), 'Gravity ad has no enabled source adlist'
        pihole_log = Path('/var/log/pihole.log')
        self.pihole_log_start = pihole_log.stat().st_size if pihole_log.exists() else 0
        return {'debian': Path('/etc/debian_version').read_text().strip(),
                'kernel': command(['uname', '-r']).stdout.strip(),
                'xray': command([self.root/'vtrui/vtrui', 'version']).stdout.splitlines()[0],
                'gravity_ad': DNS_AD, 'adlists': adlists, 'node': f'{BACKEND}:{self.args.node_port}'}

    def setup(self):
        for name in (self.lan, self.backend):
            command(['ip', 'netns', 'add', name])
            self.started_namespaces.append(name)
            self.ns(name, ['ip', 'link', 'set', 'lo', 'up'])
            resolver = Path('/etc/netns')/name
            resolver.mkdir(parents=True, exist_ok=False)
            self.resolver_directories.append(resolver)
            resolver.joinpath('resolv.conf').write_text('nameserver ' +
                (GW_LAN if name == self.lan else BACKEND) + '\noptions timeout:2 attempts:1\n')
        for name, interface, gateway, peer in (
                (self.lan, self.lan_if, GW_LAN, LAN_IP),
                (self.backend, self.up_if, GW_BACKEND, BACKEND)):
            command(['ip', 'link', 'add', interface, 'type', 'veth', 'peer', 'name', 'peer0', 'netns', name])
            command(['ip', 'addr', 'add', gateway + '/24', 'dev', interface])
            command(['ip', 'link', 'set', interface, 'up'])
            self.ns(name, ['ip', 'addr', 'add', peer + '/24', 'dev', 'peer0'])
            self.ns(name, ['ip', 'link', 'set', 'peer0', 'up'])
        self.ns(self.lan, ['ip', 'route', 'add', 'default', 'via', GW_LAN])
        self.ns(self.backend, ['ip', 'route', 'add', '192.168.231.0/24', 'via', GW_BACKEND])
        for address in TARGETS.values():
            self.ns(self.backend, ['ip', 'addr', 'add', address + '/32', 'dev', 'lo'])
        for network in ('203.0.113.0/24', '1.0.1.0/24', '10.231.0.0/24'):
            specification = ['ip', 'route', 'add', network, 'via', BACKEND, 'dev', self.up_if]
            command(specification)
            self.route_specs.append(specification)
        self.old_forward = Path('/proc/sys/net/ipv4/ip_forward').read_text().strip()
        command(['sysctl', '-q', '-w', 'net.ipv4.ip_forward=1'])
        for interface in (self.lan_if, self.up_if):
            command(['sysctl', '-q', '-w', f'net.ipv4.conf.{interface}.rp_filter=0'])
        backend_log = self.work/'backend.jsonl'
        self.processes.append(subprocess.Popen(['ip', 'netns', 'exec', self.backend, 'python3',
            str(Path(__file__).resolve()), 'backend', str(backend_log)],
            stdout=open(self.report/'backend-process.log', 'w'), stderr=subprocess.STDOUT))
        server = {'log': {'loglevel': 'info', 'access': str(self.work/'xray-access.log'),
                          'error': str(self.work/'xray-error.log')},
                  'inbounds': [{'listen': BACKEND, 'port': self.args.node_port,
                    'protocol': 'vless', 'settings': {'clients': [{'id': self.node_id}],
                    'decryption': 'none'}, 'streamSettings': {'network': 'tcp'}}],
                  'dns': {'servers': [{'address': BACKEND, 'port': 53}]},
                  'outbounds': [{'protocol': 'freedom', 'settings': {'domainStrategy': 'UseIP'}}]}
        server_path = self.work/'server.json'
        server_path.write_text(json.dumps(server))
        server_path.chmod(0o600)
        self.backend_server_argv = ['ip', 'netns', 'exec', self.backend,
            str(self.root/'vtrui/vtrui'), '-c', str(server_path)]
        self.start_backend_server()
        for _ in range(30):
            if all(process.poll() is None for process in self.processes):
                probe = self.ns(self.backend, ['curl', '--noproxy', '*', '-fsS', '--max-time', '1',
                    f'http://{TARGETS["foreign"]}:{HTTP_PORT}/readiness'], check=False)
                if probe.returncode == 0:
                    break
            time.sleep(0.1)
        else:
            raise RuntimeError('Local HTTP/Xray backend did not become ready')
        # Pin calibration rather than treating any changed source as a proxy.
        for name, address in TARGETS.items():
            entry = self.http(address, address, self.backend)
            self.calibration[name] = entry['peer']
        command(['ipset', 'test', 'chnroute', TARGETS['china']])
        command(['ipset', 'test', 'chnroute', TARGETS['force_proxy']])
        command(['ipset', 'test', 'lanip', TARGETS['private']])
        absent = command(['ipset', 'test', 'chnroute', TARGETS['foreign']], check=False)
        assert absent.returncode != 0, 'Foreign fixture is unexpectedly in the China route set'
        self.write('/etc/dnsmasq.d/99-de-gwd-integration.conf',
            f'server=/{DNS_GOOD}/{BACKEND}\nserver=/{DNS_AD}/{BACKEND}\n')
        self.touched_services = True
        command(['systemctl', 'restart', 'pihole-FTL'])
        return {'lan': LAN_IP, 'gateway': GW_LAN, 'backend': BACKEND,
                'backend_has_default_route': False, 'proxy_source_calibration': self.calibration}

    def setup_sentinels(self):
        assert '32220:' not in command(['ip', 'rule', 'show']).stdout, 'Fixture policy priority is already occupied'
        assert '192.0.2.254' not in command(['ip', 'route', 'show', 'table', '220']).stdout, \
            'Fixture table-220 prefix is already occupied'
        for table, chain, options in (
                ('filter', 'INPUT', ['-p', 'tcp', '--dport', '65123', '-j', 'ACCEPT']),
                ('mangle', 'OUTPUT', ['-p', 'udp', '--dport', '65124', '-j', 'RETURN']),
                ('nat', 'POSTROUTING', ['-d', '192.0.2.254', '-j', 'RETURN'])):
            spec = ['iptables', '-t', table, '-A', chain, '-m', 'comment', '--comment', self.tag, *options]
            command(spec)
            self.sentinel_rules.append(spec)
        command(['ip', 'rule', 'add', 'priority', '32220', 'fwmark', '0x55/0xff', 'table', '222'])
        self.marker_rule = True
        command(['ip', 'route', 'add', 'blackhole', '192.0.2.254/32', 'table', '220'])
        self.marker_route = True
        return self.sentinels()

    def sentinels(self):
        for spec in self.sentinel_rules:
            check_spec = list(spec)
            check_spec[3] = '-C'
            command(check_spec)
        rules = command(['ip', 'rule', 'show']).stdout
        routes = command(['ip', 'route', 'show', 'table', '220']).stdout
        assert '32220:' in rules and 'fwmark 0x55/0xff lookup 222' in rules, 'External policy rule was changed'
        assert 'blackhole 192.0.2.254' in routes, 'External route in table 220 was changed'
        return {'iptables_rules': len(self.sentinel_rules), 'external_policy_priority': 32220,
                'preserved_table_220_route': 'blackhole 192.0.2.254/32'}

    def toggle_proxy(self, active):
        command(['systemctl', 'start' if active else 'stop', 'iptables-proxy'])
        self.sentinels()
        return {'active': active, **self.sentinels()}

    def append_list(self, name, addresses):
        path = self.root/('IP' + name + 'SET')
        self.save(path)
        original = path.read_text() if path.exists() else ''
        path.write_text(original + ''.join(f'add {name} {address}\n' for address in addresses))

    def apply_lists(self):
        self.append_list('listW', [TARGETS['force_direct']])
        self.append_list('listB', [TARGETS['force_proxy']])
        command(['systemctl', 'restart', 'iptables-proxy'])
        self.sentinels()
        return {'listW': TARGETS['force_direct'], 'listB': TARGETS['force_proxy']}

    def source_list(self, name, target, expected):
        assert command(['ipset', 'test', name, LAN_IP], check=False).returncode != 0, \
            f'Fixture source is already a member of {name}'
        command(['ipset', 'add', name, LAN_IP])
        try:
            command(['ipset', 'test', name, LAN_IP])
            return {'source_list': name, 'source': LAN_IP, 'http': self.http(target, expected)}
        finally:
            command(['ipset', 'del', name, LAN_IP])

    def snapshot(self, name):
        for filename, argv in (
                ('iptables', ['iptables-save']),
                ('ipset', ['ipset', 'save']),
                ('policy-rules', ['ip', 'rule', 'show']),
                ('table-220', ['ip', 'route', 'show', 'table', '220'])):
            self.report.joinpath(f'{name}-{filename}.txt').write_text(command(argv).stdout)
        return {'snapshot_prefix': name}

    def good_dns(self, tcp=False):
        answer = self.dns(GW_LAN, DNS_GOOD, tcp)
        return self.require_dns(answer)

    def transparent_dns(self):
        answer = self.require_dns(self.dns(TARGETS['foreign'], DNS_GOOD))
        records = [json.loads(line) for line in self.work.joinpath('backend.jsonl').read_text().splitlines()]
        matching = [entry for entry in records if entry.get('protocol') == 'dns'
                    and entry.get('transport') == 'udp' and entry.get('query_id') == answer['query_id']]
        assert len(matching) == 1, 'Transparent DNS request lacks unique backend log evidence'
        assert matching[0]['peer'] == self.calibration['foreign'], \
            f"Transparent DNS source {matching[0]['peer']} differs from proxy source"
        return {'answer': answer, 'backend': matching[0]}

    def ad_dns(self, tcp=False):
        answer = self.dns(GW_LAN, DNS_AD, tcp)
        assert answer['rcode'] in (0, 3), str(answer)
        assert not answer['addresses'] or all(value == '0.0.0.0' for value in answer['addresses']), \
            f'Advertisement resolved to a usable address: {answer}'
        return answer

    def invalid_data(self):
        path = self.root/'chnrouteSET'
        self.save(path)
        before_rules = self.firewall_signature()
        before_sets = command(['ipset', 'save']).stdout
        before_routes = command(['ip', 'route', 'show', 'table', '220']).stdout
        original = path.read_bytes()
        try:
            path.write_text('add chnroute definitely-not-an-ip\n')
            result = command([self.root/'ui-proxyRules', 'up', 'tcp'], check=False)
            assert result.returncode != 0, 'Invalid route input unexpectedly succeeded'
            assert self.firewall_signature() == before_rules, 'Invalid route data changed live firewall'
            assert command(['ipset', 'save']).stdout == before_sets, 'Invalid route data changed live ipsets'
            assert command(['ip', 'route', 'show', 'table', '220']).stdout == before_routes, 'Invalid route data changed routes'
            self.sentinels()
            evidence = self.http(TARGETS['foreign'], self.calibration['foreign'])
            return {'exit_code': result.returncode, 'existing_proxy_still_works': evidence,
                    'existing_rules_and_sets_unchanged': True}
        finally:
            path.write_bytes(original)

    def udp_mode(self, enabled):
        self.save(self.root/'0conf')
        self.save(self.root/'iptables-proxy-up')
        command([self.root/'ui-setUDP', 'on' if enabled else 'off', 'r'])
        self.sentinels()
        return {'udp_enabled': enabled}

    def web_request(self, path, data=None, opener=None):
        encoded = urllib.parse.urlencode(data).encode() if data is not None else None
        request = urllib.request.Request(self.args.web_base_url.rstrip('/') + '/' + path, encoded)
        try:
            response = (opener or self.web).open(request, timeout=20)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, response.read().decode('utf-8', 'replace'), response.headers

    def ui_login(self):
        host = urllib.parse.urlsplit(self.args.web_base_url).hostname
        assert host in ('127.0.0.1', 'localhost', GW_LAN), 'Web checks must use the local guest UI'
        anonymous = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        status, _, _ = self.web_request('act/uptime.php', opener=anonymous)
        assert status == 403, f'Anonymous action status was {status}'
        status, _, _ = self.web_request('act/onUDP.php', opener=anonymous)
        assert status == 403, f'Anonymous mutating action status was {status}'
        self.web_request('auth.php', {'gwdpw': 'intentionally-wrong-' + uuid.uuid4().hex})
        status, _, headers = self.web_request('index.php')
        assert status == 302 and headers.get('Location', '').endswith('login.php'), 'Wrong password admitted'
        password = Path(self.args.web_password_file).read_text().rstrip('\n')
        status, _, headers = self.web_request('auth.php', {'gwdpw': password})
        del password
        assert status == 200, f'Login returned {status}'
        cookies = headers.get_all('Set-Cookie', [])
        assert any('persistentlogin=' in value and 'HttpOnly' in value and 'SameSite=Strict' in value
                   for value in cookies), 'Login cookie lacks expected security attributes'
        status, body, _ = self.web_request('act/uptime.php')
        assert status == 200 and re.fullmatch(r'(?:\d+天)?(?:\d+小时)?\d+分钟', body.strip()), \
            'Authenticated uptime action did not produce a valid response'
        return {'anonymous_read_action': 403, 'anonymous_mutating_action': 403,
                'wrong_password_denied': True, 'login_session_valid': True,
                'cookie_httponly': True, 'cookie_samesite': 'Strict', 'authenticated_action': 200}

    def ui_pages(self):
        pages = ('index.php', 'nodeMAN.php', 'listBW.php', 'app.php', 'forward.php', 'ddns.php', 'update.php', 'routing.php')
        for page in pages:
            status, body, _ = self.web_request(page)
            assert status == 200 and '<html' in body and '</html>' in body, f'{page} did not render'
            assert not any(marker in body for marker in ('Warning:', 'Fatal error:', 'Notice:')), \
                f'{page} contains a PHP diagnostic'
        return {'rendered_pages': list(pages), 'nginx_and_php_fpm': True}

    def ui_indicator(self, enabled):
        status, body, _ = self.web_request('index.php')
        assert status == 200, f'Authenticated index returned {status}'
        match = re.search(r'<button id="buttonOnUDP"[^>]*class="([^"]+)"', body)
        assert match, 'UDP indicator button is absent'
        expected = 'btn-success' if enabled else 'btn-outline-secondary'
        assert expected in match.group(1).split(), 'UDP indicator disagrees with the real runtime'
        return {'udp_enabled': enabled, 'indicator_class': expected}

    def ui_toggle(self, enabled):
        self.save(self.root/'0conf')
        self.save(self.root/'iptables-proxy-up')
        status, _, _ = self.web_request('act/onUDP.php' if enabled else 'act/offUDP.php')
        assert status == 200, f'Authenticated UDP action returned {status}'
        expected = 'success' if enabled else 'outline-secondary'
        for _ in range(100):
            actual = command([self.root/'ui-checkUDP']).stdout.strip()
            config = json.loads(self.root.joinpath('0conf').read_text())
            if actual == expected and config['address']['udp'] == ('on' if enabled else 'off'):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError('Web UDP action did not change the real proxy state')
        self.sentinels()
        return {'indicator': self.ui_indicator(enabled),
                'tcp': self.http(TARGETS['foreign'], self.calibration['foreign']),
                'udp': self.udp(TARGETS['foreign'], self.calibration['foreign'] if enabled else LAN_IP)}

    def ui_logout(self):
        self.web_request('auth.php', {'logout': 'true'})
        status, _, _ = self.web_request('act/uptime.php')
        assert status == 403, 'Logout left an authenticated action available'
        return {'after_logout_action_status': 403}

    def invalid_udp_restart(self):
        path = self.root/'chnrouteSET'
        original = path.read_bytes()
        old_config = self.root.joinpath('0conf').read_bytes()
        old_wrapper = self.root.joinpath('iptables-proxy-up').read_bytes()
        old_rules = self.firewall_signature()
        try:
            path.write_text('add chnroute invalid-test-address\n')
            result = command([self.root/'ui-setUDP', 'off', 'r'], check=False)
            assert result.returncode != 0, 'Invalid UDP restart unexpectedly succeeded'
            assert self.root.joinpath('0conf').read_bytes() == old_config, 'Failed UDP toggle changed config'
            assert self.root.joinpath('iptables-proxy-up').read_bytes() == old_wrapper, 'Failed UDP toggle changed wrapper'
            assert self.firewall_signature() == old_rules, 'Failed UDP toggle changed live firewall'
            self.sentinels()
            return {'exit_code': result.returncode, 'old_udp_configuration_preserved': True,
                    'existing_proxy': self.udp(TARGETS['foreign'], self.calibration['foreign'])}
        finally:
            path.write_bytes(original)

    def restart_services(self):
        units = []
        for name in ('vtrui', 'pihole-FTL', 'nginx', 'php8.4-fpm', 'iptables-proxy'):
            if command(['systemctl', 'cat', name], check=False).returncode == 0:
                command(['systemctl', 'restart', name])
                command(['systemctl', 'is-active', '--quiet', name])
                units.append(name)
        self.sentinels()
        return {'restarted_units': units,
                'http': self.http(TARGETS['foreign'], self.calibration['foreign']),
                'udp': self.udp(TARGETS['foreign'], self.calibration['foreign']),
                'dns_normal': self.good_dns(), 'dns_ad': self.ad_dns()}

    def start_backend_server(self):
        self.backend_server = subprocess.Popen(self.backend_server_argv,
            stdout=open(self.report/'xray-process.log', 'a'), stderr=subprocess.STDOUT)
        self.processes.append(self.backend_server)
        for _ in range(50):
            listener = self.ns(self.backend, ['ss', '-H', '-lnt', f'sport = :{self.args.node_port}'])
            if listener.stdout.strip():
                return
            if self.backend_server.poll() is not None:
                raise RuntimeError('Controlled upstream VLESS server exited during startup')
            time.sleep(0.1)
        raise RuntimeError('Controlled upstream VLESS server is not listening')

    def upstream_failure_negative_control(self):
        self.backend_server.terminate()
        self.backend_server.wait(timeout=3)
        try:
            result = self.ns(self.lan, ['env', '-i', 'PATH=/usr/bin:/bin', 'curl', '-q', '-4',
                '--noproxy', '*', '-fsS', '--max-time', '3',
                f'http://{TARGETS["foreign"]}:{HTTP_PORT}/{uuid.uuid4().hex}'], check=False, timeout=5)
            assert result.returncode != 0, 'Foreign traffic succeeded with upstream VLESS unavailable'
            return {'foreign_request_exit_code': result.returncode,
                    'china_still_direct': self.http(TARGETS['china'], LAN_IP),
                    'private_still_direct': self.http(TARGETS['private'], LAN_IP)}
        finally:
            self.start_backend_server()

    def dns_log_evidence(self):
        log = Path('/var/log/pihole.log')
        assert log.exists(), 'Pi-hole query log is absent; blocked answer lacks server log evidence'
        with log.open() as stream:
            stream.seek(self.pihole_log_start)
            content = stream.read()
        relevant = [line for line in content.splitlines() if DNS_GOOD in line or DNS_AD in line]
        self.report.joinpath('pihole-queries.log').write_text('\n'.join(relevant) + '\n')
        assert any(DNS_GOOD in line and LAN_IP in line for line in relevant), 'Normal LAN DNS query not logged'
        assert any(DNS_AD in line and LAN_IP in line for line in relevant), 'Ad LAN DNS query not logged'
        assert any(DNS_AD in line and 'gravity' in line for line in relevant), 'Ad reply not logged as gravity blocked'
        return {'query_client': LAN_IP, 'normal_domain': DNS_GOOD, 'gravity_blocked_domain': DNS_AD,
                'evidence_file': 'pihole-queries.log'}

    def cleanup(self):
        errors = []
        for path, backup in reversed(list(self.backups.items())):
            try:
                if backup is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(backup[0])
                    path.chmod(backup[1])
            except Exception as exc:
                errors.append(f'restore {path}: {exc}')
        if self.touched_services:
            for unit in ('pihole-FTL', 'iptables-proxy'):
                result = command(['systemctl', 'restart', unit], check=False)
                if result.returncode:
                    errors.append(f'restore service {unit}: {result.stderr.strip()}')
        for source, target in (('backend.jsonl', 'backend-access.jsonl'),
                               ('xray-access.log', 'upstream-xray-access.log'),
                               ('xray-error.log', 'upstream-xray-error.log')):
            if (self.work/source).exists():
                shutil.copyfile(self.work/source, self.report/target)
        for process in self.processes:
            process.terminate()
        for process in self.processes:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for name in reversed(self.started_namespaces):
            result = command(['ip', 'netns', 'delete', name], check=False)
            if result.returncode:
                errors.append(f'remove namespace {name}: {result.stderr.strip()}')
        for directory in self.resolver_directories:
            shutil.rmtree(directory)
        for spec in self.sentinel_rules:
            delete_spec = list(spec)
            delete_spec[3] = '-D'
            result = command(delete_spec, check=False)
            if result.returncode:
                errors.append(f'remove sentinel: {result.stderr.strip()}')
        if self.marker_rule:
            result = command(['ip', 'rule', 'del', 'priority', '32220', 'fwmark', '0x55/0xff', 'table', '222'], check=False)
            if result.returncode:
                errors.append(f'remove policy sentinel: {result.stderr.strip()}')
        if self.marker_route:
            result = command(['ip', 'route', 'del', 'blackhole', '192.0.2.254/32', 'table', '220'], check=False)
            if result.returncode:
                errors.append(f'remove route sentinel: {result.stderr.strip()}')
        if self.old_forward is not None:
            result = command(['sysctl', '-q', '-w', f'net.ipv4.ip_forward={self.old_forward}'], check=False)
            if result.returncode or Path('/proc/sys/net/ipv4/ip_forward').read_text().strip() != self.old_forward:
                errors.append('restore original ip_forward failed')
        remaining = command(['ip', 'netns', 'list']).stdout
        if any(name in remaining for name in self.started_namespaces):
            errors.append('A test namespace still exists after cleanup')
        if self.tag in command(['iptables-save']).stdout:
            errors.append('A test firewall sentinel still exists after cleanup')
        shutil.rmtree(self.work)
        if errors:
            raise RuntimeError('; '.join(errors))
        return {'temporary_configuration_restored': True, 'namespaces_removed': True}

    def run(self):
        try:
            if not self.check('debian11_real_service_preflight', self.preflight):
                return 1
            if not self.check('isolated_topology_and_exact_source_calibration', self.setup):
                return 1
            if not self.check('external_firewall_and_policy_route_sentinels', self.setup_sentinels):
                return 1
            self.check('initial_live_kernel_snapshot', lambda: self.snapshot('initial'))
            self.check('stop_preserves_external_firewall', lambda: self.toggle_proxy(False))
            for name, address in TARGETS.items():
                self.check('direct_baseline_' + name, lambda a=address: self.http(a, LAN_IP))
                self.check('direct_udp_baseline_' + name, lambda a=address: self.udp(a, LAN_IP))
            self.check('start_preserves_external_firewall', lambda: self.toggle_proxy(True))
            self.check('lan_tcp_transparent_proxy_without_proxy_variables',
                       lambda: self.http(TARGETS['foreign'], self.calibration['foreign']))
            self.check('china_route_tcp_direct', lambda: self.http(TARGETS['china'], LAN_IP))
            self.check('private_tcp_direct', lambda: self.http(TARGETS['private'], LAN_IP))
            self.check('upstream_vless_failure_negative_control', self.upstream_failure_negative_control)
            self.check('upstream_restored_tcp_proxy',
                       lambda: self.http(TARGETS['foreign'], self.calibration['foreign']))
            self.check('apply_custom_destination_lists', self.apply_lists)
            self.check('custom_force_direct_tcp', lambda: self.http(TARGETS['force_direct'], LAN_IP))
            self.check('custom_force_proxy_overrides_china_tcp',
                       lambda: self.http(TARGETS['force_proxy'], self.calibration['force_proxy']))
            self.check('source_force_direct_overrides_foreign_tcp',
                       lambda: self.source_list('listWlan', TARGETS['foreign'], LAN_IP))
            self.check('source_force_proxy_overrides_china_tcp',
                       lambda: self.source_list('listBlan', TARGETS['china'], self.calibration['china']))
            self.check('tcp_live_kernel_snapshot', lambda: self.snapshot('tcp'))
            self.check('invalid_route_configuration_preserves_existing_proxy', self.invalid_data)
            self.check('tcp_mode_udp_is_direct', lambda: self.udp(TARGETS['foreign'], LAN_IP))
            if self.args.web_password_file:
                self.check('real_nginx_php_ui_authentication_and_action', self.ui_login)
                self.check('real_nginx_php_ui_page_rendering', self.ui_pages)
                self.check('ui_udp_disabled_indicator_matches_runtime', lambda: self.ui_indicator(False))
                self.check('ui_enable_udp_real_action_and_traffic', lambda: self.ui_toggle(True))
            else:
                self.check('enable_udp_real_runtime', lambda: self.udp_mode(True))
            self.check('lan_udp_transparent_proxy', lambda: self.udp(TARGETS['foreign'], self.calibration['foreign']))
            self.check('china_route_udp_direct', lambda: self.udp(TARGETS['china'], LAN_IP))
            self.check('private_udp_direct', lambda: self.udp(TARGETS['private'], LAN_IP))
            self.check('custom_force_direct_udp', lambda: self.udp(TARGETS['force_direct'], LAN_IP))
            self.check('custom_force_proxy_overrides_china_udp',
                       lambda: self.udp(TARGETS['force_proxy'], self.calibration['force_proxy']))
            self.check('udp_transparent_dns_packet',
                       self.transparent_dns)
            self.check('controlled_authority_resolves_ad_without_gravity',
                       lambda: self.require_dns(self.dns(BACKEND, DNS_AD)))
            self.check('pihole_normal_udp_dns', self.good_dns)
            self.check('pihole_normal_tcp_dns', lambda: self.good_dns(True))
            self.check('pihole_ad_udp_dns_blocked', self.ad_dns)
            self.check('pihole_ad_tcp_dns_blocked', lambda: self.ad_dns(True))
            self.check('normal_dns_result_http_access', self.normal_dns_http)
            self.check('pihole_query_log_proves_lan_gravity_block', self.dns_log_evidence)
            if self.args.web_password_file:
                self.check('ui_disable_udp_real_action_and_traffic', lambda: self.ui_toggle(False))
                self.check('ui_reenable_udp_real_action_and_traffic', lambda: self.ui_toggle(True))
            self.check('failed_udp_mode_change_preserves_live_runtime', self.invalid_udp_restart)
            self.check('client_services_restart_and_functional_recheck', self.restart_services)
            self.check('repeated_proxy_start_is_idempotent', self.repeat_up)
            self.check('udp_live_kernel_snapshot', lambda: self.snapshot('udp'))
            if self.args.web_password_file:
                self.check('ui_indicator_after_client_service_restart', lambda: self.ui_indicator(True))
                self.check('ui_logout_invalidates_action_access', self.ui_logout)
        finally:
            self.check('cleanup_restores_configuration_and_removes_fixtures', self.cleanup)
            self.finish_report()
        return int(any(row['status'] != 'passed' for row in self.results))

    @staticmethod
    def require_dns(answer):
        assert answer['rcode'] == 0 and answer['addresses'] == [TARGETS['foreign']], str(answer)
        return answer

    def normal_dns_http(self):
        answer = self.good_dns()
        return {'dns': answer, 'http': self.http(DNS_GOOD, self.calibration['foreign'],
                                               expected_local=TARGETS['foreign']),
                'lan_resolver': GW_LAN}

    def repeat_up(self):
        before = self.firewall_signature()
        command([self.root/'ui-proxyRules', 'up', 'tcpudp'])
        command([self.root/'ui-proxyRules', 'up', 'tcpudp'])
        assert self.firewall_signature() == before, 'Repeated start duplicated or changed rules'
        self.sentinels()
        return {'http': self.http(TARGETS['foreign'], self.calibration['foreign']),
                'udp': self.udp(TARGETS['foreign'], self.calibration['foreign'])}

    @staticmethod
    def firewall_signature():
        # The save format includes timestamps and packet counters; those are
        # observations, not changes to firewall rule ownership or ordering.
        return '\n'.join(re.sub(r'\[\d+:\d+\]', '[counters]', line)
                         for line in command(['iptables-save']).stdout.splitlines()
                         if not line.startswith('#'))

    def finish_report(self):
        passed = sum(row['status'] == 'passed' for row in self.results)
        failed = len(self.results) - passed
        lines = ['# Debian 11 controlled integration result', '',
                 f'Executed at {datetime.datetime.now(datetime.timezone.utc).isoformat()}.', '',
                 f'{passed} passed; {failed} failed; {len(self.results)} executed checks.', '',
                 'Scope: real Debian 11 kernel, systemd, Xray, iptables TPROXY, ipset and Pi-hole.',
                 'All targets and the VLESS backend are inside isolated guest network namespaces.',
                 'The backend has no default route. No remote server or public TLS endpoint was tested.',
                 'Direct baseline and exact backend source calibration precede proxy assertions.', '',
                 '| Check | Result |', '| --- | --- |']
        lines += [f'| {row["name"]} | {row["status"]} |' for row in self.results]
        lines += ['', 'Detailed evidence: `results.json`, `backend-access.jsonl`,',
                  '`upstream-xray-access.log`, `upstream-xray-error.log`.', '',
                  'Limits: this verifies IPv4 TCP/UDP and gateway DNS, with controlled plaintext VLESS.',
                  'It does not verify the inaccessible remote server, public TLS/ACME, IPv6,',
                  'or enforcement against clients choosing a different DNS server.',
                  'Firewall sentinels cover unrelated INPUT/OUTPUT/POSTROUTING rules, a different',
                  'policy rule, and a specific-prefix route in table 220; conflicting ownership',
                  'of the proxy reserved mark or table-220 local default is outside this fixture.']
        for row in self.results:
            if row['status'] == 'failed':
                lines += ['', f'Failure `{row["name"]}`: {row["error"]}']
        self.report.joinpath('report.md').write_text('\n'.join(lines) + '\n')
        print(f'Report: {self.report}/report.md', flush=True)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'backend':
        run_backend(sys.argv[2])
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == 'dns':
        print(json.dumps(probe_dns(sys.argv[2], sys.argv[3], sys.argv[4] == 'tcp')))
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == 'udp':
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(5)
            sock.sendto(sys.argv[3].encode('ascii'), (sys.argv[2], UDP_PORT))
            response, peer = sock.recvfrom(4096)
            assert peer == (sys.argv[2], UDP_PORT), f'Unexpected UDP responder: {peer}'
            print(response.decode('ascii'))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='/opt/de_GWD')
    parser.add_argument('--node-port', type=int, default=14443)
    parser.add_argument('--report-dir', required=True)
    parser.add_argument('--web-password-file', help='Read a local fixture UI password without logging it')
    parser.add_argument('--web-base-url', default='http://127.0.0.1')
    args = parser.parse_args()
    return Suite(args).run()


if __name__ == '__main__':
    sys.exit(main())
