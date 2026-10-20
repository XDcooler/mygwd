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
                    '/run/lock': str(self.root/'locks'), '/lib/systemd/': str(self.root/'lib/systemd')+'/'}
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
        result = self.shell('domain=example.test; port='+str(port)+'; path=/fixture; nginxWebConf')
        self.assertEqual(result.returncode, 0, result.stderr)
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
