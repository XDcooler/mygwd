"""Exercise server entry points with isolated files and command adapters."""
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
import unittest
import urllib.error
from urllib.parse import parse_qs, urlsplit
import urllib.request
import zipfile

REPO = Path(__file__).resolve().parents[1]
STAGES = ('preInstall installPihole piholeSet installIptablesRules installDOH '
          'installNginx installXray makeSSL_W ocspStapling nginxWebConf '
          'xrayInbound xrayOutboundDirect postInstall').split()


class ServerInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mygwd-server-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.events = self.root/'events'
        self.events.touch()
        source = (REPO/'server').read_text().rsplit('\ncase "${1:-}" in\n', 1)[0]
        mappings = {'/opt/de_GWD': str(self.root/'gateway'), '/etc/': str(self.root/'etc')+'/',
                    '/var/www/': str(self.root/'www')+'/', '/tmp/': str(self.root/'tmp')+'/',
                    '/run/mygwd-server': str(self.root/'locks'), '/lib/systemd/': str(self.root/'lib/systemd')+'/',
                    '/root/': str(self.root/'root')+'/'}
        for index, old in enumerate(mappings):
            source = source.replace(old, '__FIXTURE_'+str(index)+'__')
        for index, new in enumerate(mappings.values()):
            source = source.replace('__FIXTURE_'+str(index)+'__', new)
        self.source = self.root/'server'
        self.source.write_text(source)
        for directory in ('tmp', 'gateway/vtrui', 'etc/systemd/system', 'lib/systemd/system', 'locks'):
            (self.root/directory).mkdir(parents=True, exist_ok=True)
        (self.root/'gateway/role').write_text('server\n')

    def shell(self, body, **env):
        prefix = '''
dpkg() { echo amd64; }
systemd-detect-virt() { echo kvm; }
source "$1"
requireRoot() { return 0; }
requireDebian13() { return 0; }
flock() { return 0; }
systemctl() { echo "systemctl $*" >>"$EVENTS"; }
service() { echo "service $*" >>"$EVENTS"; }
ip() { if [[ $1 == --oneline ]]; then echo '2: eth0: UP'; else echo 'inet 192.0.2.10/24 scope global eth0'; fi; }
cat() { if [[ ${1:-} == /proc/sys/kernel/random/uuid ]]; then echo 11111111-1111-4111-8111-111111111111; else command cat "$@"; fi; }
'''
        environment = dict(os.environ, EVENTS=str(self.events), FAIL_STAGE='')
        environment.update(env)
        return subprocess.run(['bash', '-c', prefix+body, 'fixture', str(self.source)],
            env=environment,
            capture_output=True, text=True, timeout=30)

    def stages(self):
        return '\n'.join(name+'() { echo '+name+' >>"$EVENTS"; [[ $FAIL_STAGE != '+name+' ]]; }'
                         for name in STAGES) + '\nprintNode() { echo printNode >>"$EVENTS"; }\n'

    def node_fixture(self, listeners='listen 443 ssl http2 fastopen=128 reuseport;',
                     path='/fixture', domain='node.example.com', extra=''):
        directory = self.root/'etc/nginx/conf.d'
        directory.mkdir(parents=True, exist_ok=True)
        configuration = directory/'default.conf'
        configuration.write_text(f'''# listen 9999 ssl; server_name ignored.example;
server {{ listen 80; server_name redirect.example; }}
server {{
    {listeners}
    server_name {domain};
    add_header X-Test "quoted ; {{ }} # text";
    location /fixture {{
        if ($http_upgrade != "websocket") {{ return 404; }}
        proxy_set_header Host ${{host}};
    }}
}}
{extra}
''')
        config = {'inbounds': [{'protocol': 'vless',
            'settings': {'clients': [{'id': '11111111-1111-4111-8111-111111111111'}]},
            'streamSettings': {'network': 'ws', 'wsSettings': {'path': path}}}]}
        (self.root/'gateway/vtrui/config.json').write_text(json.dumps(config))
        return configuration, config

    def node_output(self, body='printNode', **environment):
        capture = self.root/'qr-arguments'
        capture.unlink(missing_ok=True)
        result = self.shell('''
qrencode() { printf '%s\\0' "$@" >"$QR_ARGUMENTS"; return "${QR_STATUS:-0}"; }
'''+body, QR_ARGUMENTS=str(capture), **environment)
        arguments = capture.read_bytes().decode().rstrip('\0').split('\0') if capture.exists() else []
        return result, arguments

    def test_node_url_always_has_the_explicit_https_port(self):
        for listener, expected in (
            ('listen 443 ssl http2 fastopen=128 reuseport;', 443),
            ('listen 8443 ssl http2 fastopen=128 reuseport;', 8443),
            ('listen 443 ssl; http2 on;', 443),
            ('listen 8443 reuseport ssl fastopen=128 http2;', 8443),
            ('listen 0.0.0.0:8443 ssl;', 8443),
            ('listen [::]:8443 ssl;', 8443),
            ('listen 443 ssl; listen [::]:443 ssl ipv6only=on;', 443),
            ('listen\n "8443"\n ssl default_server; http2 on;', 8443),
        ):
            with self.subTest(listener=listener):
                self.node_fixture(listeners=listener)
                result, arguments = self.node_output()
                self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
                self.assertEqual(arguments[:6], ['-t', 'UTF8', '-s', '1', '-m', '3'])
                self.assertEqual(len(arguments), 7)
                uri = urlsplit(arguments[-1])
                self.assertEqual(uri.scheme, 'vless')
                self.assertEqual(uri.hostname, 'node.example.com')
                self.assertEqual(uri.port, expected)
                self.assertIn('URL: '+arguments[-1]+'\n', result.stdout)

    def test_node_query_values_round_trip_without_changing_the_path(self):
        for path in ('/fixture', '/route?ed=2048&token=a+b#fragment', '/空 格/%2F'):
            with self.subTest(path=path):
                self.node_fixture(path=path)
                result, arguments = self.node_output()
                self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
                self.assertEqual(len(arguments), 7)
                uri = urlsplit(arguments[-1])
                self.assertFalse(uri.fragment)
                self.assertFalse(any(character.isspace() for character in arguments[-1]))
                self.assertEqual(parse_qs(uri.query), {'encryption': ['none'], 'security': ['tls'],
                    'sni': ['node.example.com'], 'type': ['ws'], 'path': [path], 'tfo': ['1'], 'mux': ['1']})
        self.node_fixture()
        result, _ = self.node_output()
        self.assertNotIn('/fixture?ed=2048', result.stdout)

    def test_invalid_or_ambiguous_endpoints_do_not_print_a_url_or_qr(self):
        for listeners, domain, extra in (
            ('listen 80;', 'node.example.com', ''),
            ('listen 0 ssl;', 'node.example.com', ''),
            ('listen 65536 ssl;', 'node.example.com', ''),
            ('listen 192.0.2.10 ssl;', 'node.example.com', ''),
            ('listen 443 ssl; listen 8443 ssl;', 'node.example.com', ''),
            ('listen 443 ssl;', '_ *.example.com', ''),
            ('listen 443 ssl;', 'node.example.com',
             'server { listen 9443 ssl; server_name other.example.com; }'),
        ):
            with self.subTest(listeners=listeners, domain=domain, extra=extra):
                self.node_fixture(listeners=listeners, domain=domain, extra=extra)
                result, arguments = self.node_output()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Cannot read the public server endpoint', result.stderr)
                self.assertNotIn('URL:', result.stdout)
                self.assertFalse(arguments)

    def test_missing_or_unfinished_nginx_config_cannot_generate_a_node(self):
        configuration, _ = self.node_fixture()
        for content in (None, 'server { listen 443 ssl; server_name node.example.com;',
                        'server { listen 443 ssl; server_name node.example.com }'):
            with self.subTest(content=content):
                if content is None:
                    configuration.unlink()
                else:
                    configuration.write_text(content)
                result, arguments = self.node_output()
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(arguments)

    def test_invalid_node_credentials_or_path_do_not_generate_a_qr(self):
        for field, value in (('uuid', ''), ('uuid', None), ('uuid', 'not-a-uuid'),
                             ('path', ''), ('path', None), ('path', 'fixture'), ('path', '/bad\npath')):
            with self.subTest(field=field, value=value):
                _, config = self.node_fixture()
                inbound = config['inbounds'][0]
                if field == 'uuid':
                    inbound['settings']['clients'][0]['id'] = value
                else:
                    inbound['streamSettings']['wsSettings']['path'] = value
                (self.root/'gateway/vtrui/config.json').write_text(json.dumps(config))
                result, arguments = self.node_output()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Cannot generate the node URL', result.stderr)
                self.assertNotIn('URL:', result.stdout)
                self.assertFalse(arguments)

    def test_missing_or_corrupt_node_config_does_not_generate_a_qr(self):
        self.node_fixture()
        configuration = self.root/'gateway/vtrui/config.json'
        for content in (None, '{bad json', '{}', '{"inbounds": []}'):
            with self.subTest(content=content):
                if content is None:
                    configuration.unlink()
                else:
                    configuration.write_text(content)
                result, arguments = self.node_output()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Cannot generate the node URL', result.stderr)
                self.assertNotIn('URL:', result.stdout)
                self.assertFalse(arguments)

    def test_qr_encoder_failure_is_reported(self):
        self.node_fixture()
        result, arguments = self.node_output(QR_STATUS='23')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(arguments), 7)

    def test_update_rejects_an_unknown_port_before_mutating_configuration(self):
        configuration, _ = self.node_fixture(listeners='listen 80;')
        before = configuration.read_bytes()
        result = self.shell('preUpdate')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(configuration.read_bytes(), before)
        self.assertEqual(self.events.read_text(), '')

    def test_update_preserves_a_custom_port_from_a_modern_listener(self):
        self.node_fixture(listeners='listen [::]:8443 ssl; http2 on;')
        (self.root/'www/ssl').mkdir(parents=True)
        for name in ('de_GWD.key', 'de_GWD.cer'):
            (self.root/'www/ssl'/name).touch()
        result = self.shell('''
crontab() { :; }
preUpdate || exit $?
printf 'ENDPOINT=%s:%s\\n' "$domain" "$port"
''')
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertIn('ENDPOINT=node.example.com:8443', result.stdout)

    def test_changing_uuid_preserves_the_existing_https_port(self):
        self.node_fixture(listeners='listen 8443 ssl; http2 on;')
        result = self.shell('''
nginxWebConf() { printf 'REBUILD=%s:%s\\n' "$domain" "$port"; }
xrayInbound() { :; }
xrayOutboundDirect() { :; }
changeXrayVerify <<'INPUT'
22222222-2222-4222-8222-222222222222
/newpath
INPUT
''')
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertIn('REBUILD=node.example.com:8443', result.stdout)

    def test_changing_uuid_rejects_an_unknown_port_before_rebuilding(self):
        self.node_fixture(listeners='listen 80;')
        result = self.shell('''
nginxWebConf() { echo unexpected-rebuild >>"$EVENTS"; }
changeXrayVerify </dev/null
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.events.read_text(), '')

    def test_each_install_failure_stops_later_stages_and_success_output(self):
        for failed in STAGES:
            with self.subTest(stage=failed):
                self.events.write_text('')
                result = self.shell(self.stages()+'installGWD example.com', FAIL_STAGE=failed)
                self.assertNotEqual(result.returncode, 0, result.stdout+result.stderr)
                self.assertEqual(self.events.read_text().splitlines(), STAGES[:STAGES.index(failed)+1])
                self.assertNotIn('[done]', result.stdout)

    def test_successful_install_runs_all_stages_then_prints_the_node(self):
        result = self.shell(self.stages()+'installGWD example.com')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events.read_text().splitlines(), STAGES+['printNode'])
        self.assertIn('Install de_GWD [done]', result.stdout)

    def test_invalid_domains_and_ports_abort_before_installation(self):
        for domain in ('example.com:0', 'example.com:65536', 'example.com:443:80',
                       '-bad.example.com', 'example..com', 'example.com;touch marker'):
            with self.subTest(domain=domain):
                result = self.shell(self.stages()+'installGWD "$INPUT_DOMAIN"', INPUT_DOMAIN=domain)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.events.read_text(), '')

    def test_missing_domain_on_eof_does_not_install(self):
        result = self.shell(self.stages()+'installGWD </dev/null')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.events.read_text(), '')

    def test_update_failure_restores_cron_and_reports_the_failed_stage(self):
        result = self.shell(self.stages()+'''
preUpdate() { serverCronStopped=1; }
runServerOperation updateGWD
''', FAIL_STAGE='installXray')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('service cron restart', self.events.read_text())
        self.assertNotIn('nginxWebConf', self.events.read_text())
        self.assertIn('failed at installXray', result.stdout+result.stderr)
        self.assertNotIn('[done]', result.stdout)

    def test_update_failure_stops_optional_forwarding_stages(self):
        result = self.shell(self.stages()+'''
preUpdate() { xtlsPort=8443; upDomain=up.example.com; }
xrayInboundForward() { echo forward >>"$EVENTS"; return 1; }
xrayOutboundForward() { echo upstream >>"$EVENTS"; }
updateGWD
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('upstream', self.events.read_text())
        self.assertNotIn('postInstall', self.events.read_text())

    def test_dependency_failures_do_not_reach_profile_or_upgrade_the_os(self):
        (self.root/'etc/apt/apt.conf.d').mkdir(parents=True)
        (self.root/'etc/profile').write_text('echo profile >>"$EVENTS"; exit 97\n')
        for failure in ('update', 'install'):
            with self.subTest(failure=failure):
                self.events.write_text('')
                result = self.shell('''
virtVC=container
free() { printf 'header\\nMemory: 1\\nSwap: 1\\n'; }
dpkg() { return 0; }
ps() { return 0; }
apt() { echo "apt $*" >>"$EVENTS"; [[ $1 != "$FAILED_APT" ]]; }
preInstall
''', FAILED_APT=failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('profile', self.events.read_text())
                self.assertNotIn('upgrade', self.events.read_text())
                self.assertNotIn('autoremove', self.events.read_text())

    def certificate_request(self, fail='0'):
        ssl_directory = self.root/'www/ssl'
        ssl_directory.mkdir(parents=True)
        (ssl_directory/'dhparam.pem').touch()
        acme = self.root/'acme-mock'
        acme.write_text('''#!/bin/bash
echo "acme $*" >>"$EVENTS"
if [[ $1 == --issue && $FAIL_CERT == 1 ]]; then exit 1; fi
if [[ $1 == --installcert ]]; then touch "$CERT_DIRECTORY/de_GWD.key" "$CERT_DIRECTORY/de_GWD.cer"; fi
''')
        return self.shell('''
curl() { return 0; }
sh() { mkdir -p "$ACME_HOME"; cp "$ACME_MOCK" "$ACME_HOME/acme.sh"; chmod +x "$ACME_HOME/acme.sh"; }
crontab() { return 0; }
chown() { return 0; }
domain=edge.example.co.uk; topDomain=co.uk
CFapikey=fixture; CFemail=fixture@example.test
makeSSL_D
''', ACME_MOCK=str(acme), ACME_HOME=str(self.root/'root/.acme.sh'),
            CERT_DIRECTORY=str(ssl_directory), FAIL_CERT=fail)

    def test_dns_certificate_uses_the_full_hostname_for_multilevel_suffixes(self):
        result = self.certificate_request()
        self.assertEqual(result.returncode, 0, result.stderr)
        events = self.events.read_text()
        self.assertIn('--issue --dns dns_cf -d edge.example.co.uk -d *.edge.example.co.uk', events)
        self.assertIn('--installcert -d edge.example.co.uk --ecc', events)
        self.assertNotIn('-d co.uk', events)

    def test_failed_dns_certificate_request_does_not_install_a_certificate(self):
        result = self.certificate_request('1')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('--installcert', self.events.read_text())

    def core_install(self, archive):
        core = self.root/'gateway/vtrui/vtrui'
        core.write_bytes(b'previous core')
        result = self.shell('copyOrDownloadAsset() { cp "$ARCHIVE" "$2"; }; installXray', ARCHIVE=str(archive))
        return result, core

    def test_invalid_core_archive_keeps_the_existing_executable(self):
        archive = self.root/'bad.zip'
        archive.write_bytes(b'not a zip')
        result, core = self.core_install(archive)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(core.read_bytes(), b'previous core')
        self.assertEqual(self.events.read_text(), '')

    def test_missing_or_empty_core_members_do_not_replace_the_core(self):
        for members in ({'xray': b'core'}, {'xray': b'', 'geoip.dat': b'geo', 'geosite.dat': b'site'}):
            with self.subTest(members=list(members)):
                archive = self.root/'incomplete.zip'
                with zipfile.ZipFile(archive, 'w') as package:
                    for name, data in members.items(): package.writestr(name, data)
                result, core = self.core_install(archive)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(core.read_bytes(), b'previous core')

    def test_valid_core_archive_installs_the_core_and_service_unit(self):
        archive = self.root/'valid.zip'
        with zipfile.ZipFile(archive, 'w') as package:
            for name in ('xray', 'geoip.dat', 'geosite.dat'): package.writestr(name, b'valid '+name.encode())
        result, core = self.core_install(archive)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(core.read_bytes(), b'valid xray')
        self.assertTrue(core.stat().st_mode & 0o111)
        self.assertIn('User=www-data', (self.root/'etc/systemd/system/vtrui.service').read_text())

    @unittest.skipUnless(platform.system() == 'Linux' and shutil.which('jq') and shutil.which('sponge'),
                         'Requires Linux with jq and moreutils for the real proxy-core check')
    def test_generated_config_is_accepted_by_the_packaged_xray_core(self):
        architecture = {'x86_64': 'amd64', 'aarch64': 'arm64'}[platform.machine()]
        executable = self.root/'xray'
        with zipfile.ZipFile(REPO/f'resource/vtrui_{architecture}.zip') as package:
            executable.write_bytes(package.read('xray'))
        executable.chmod(0o755)
        result = self.shell('uuids=11111111-1111-4111-8111-111111111111; path=/fixture; xrayInbound && xrayOutboundDirect')
        self.assertEqual(result.returncode, 0, result.stderr)
        config = self.root/'gateway/vtrui/config.json'
        parsed = json.loads(config.read_text())
        self.assertEqual(parsed['inbounds'][0]['streamSettings']['wsSettings']['path'], '/fixture')
        check = subprocess.run([str(executable), '-test', '-config', str(config)], capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stdout+check.stderr)

    @unittest.skipUnless(platform.system() == 'Linux' and shutil.which('nginx') and shutil.which('openssl'),
                         'Requires Linux nginx and OpenSSL for real HTTPS validation')
    def test_generated_nginx_configuration_serves_verified_https(self):
        self.root.chmod(0o755)
        ssl_directory = self.root/'www/ssl'
        ssl_directory.mkdir(parents=True)
        web = self.root/'www/html'
        web.mkdir()
        (web/'index.html').write_text('mygwd HTTPS fixture')
        (self.root/'etc/nginx/conf.d').mkdir(parents=True)
        subprocess.run(['openssl', 'req', '-x509', '-nodes', '-newkey', 'rsa:2048', '-days', '1',
            '-subj', '/CN=example.test', '-addext', 'subjectAltName=DNS:example.test,IP:127.0.0.1',
            '-keyout', str(ssl_directory/'de_GWD.key'), '-out', str(ssl_directory/'de_GWD.cer')],
            check=True, capture_output=True)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        result = self.shell('domain=example.test; port='+str(port)+'''; path=/fixture
nginxWebConf && readServerEndpoint && printf 'ENDPOINT=%s:%s\\n' "$domain" "$port"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'ENDPOINT=example.test:{port}', result.stdout)
        configuration = self.root/'nginx.conf'
        configuration.write_text(f'pid {self.root}/nginx.pid;\nerror_log stderr;\nevents {{}}\n'
            f'http {{ access_log off; include {self.root}/etc/nginx/conf.d/*.conf; }}\n')
        command = ['nginx', '-p', str(self.root), '-c', str(configuration)]
        check = subprocess.run(command+['-t'], capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stderr)
        with (self.root/'nginx.log').open('w') as log:
            process = subprocess.Popen(command+['-g', 'daemon off;'], stdout=log, stderr=log)
            try:
                context = ssl.create_default_context(cafile=str(ssl_directory/'de_GWD.cer'))
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))
                for attempt in range(30):
                    try:
                        with opener.open(f'https://127.0.0.1:{port}/', timeout=1) as response:
                            self.assertEqual(response.status, 200)
                            self.assertEqual(response.read(), b'mygwd HTTPS fixture')
                        break
                    except urllib.error.URLError:
                        if attempt == 29: raise
                        time.sleep(.1)
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    opener.open(f'https://127.0.0.1:{port}/admin/', timeout=2)
                self.assertEqual(denied.exception.code, 404)
            finally:
                process.terminate()
                process.wait(timeout=5)

    def test_help_and_invalid_option_have_finite_exit_codes(self):
        help_result = subprocess.run(['bash', str(REPO/'server'), '--help'], capture_output=True, text=True, timeout=5)
        self.assertEqual(help_result.returncode, 0)
        self.assertIn('--update-kernel', help_result.stdout)
        invalid = subprocess.run(['bash', str(REPO/'server'), '--unknown'], capture_output=True, timeout=5)
        self.assertEqual(invalid.returncode, 2)


if __name__ == '__main__': unittest.main(verbosity=2)
