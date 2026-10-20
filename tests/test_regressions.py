"""Exercise actual PHP endpoints in private mount namespaces or PRoot with fake commands."""
import hashlib
import hmac
import http.cookiejar
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile

REPO = Path(os.environ.get('DE_GWD_SOURCE_ROOT', Path(__file__).resolve().parents[1]))
WEB = REPO / 'resource/client/ui-web'
ROOTFS = Path(os.environ.get('DE_GWD_ROOTFS', '/'))
PASSWORD = 'local-regression-fixture'
PASSWORD_HASH = hashlib.sha256(hashlib.sha256(PASSWORD.encode()).hexdigest().encode()).hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class RegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        native = ROOTFS.resolve() == Path('/')
        if native and (os.geteuid() != 0 or shutil.which('unshare') is None):
            raise RuntimeError('Native VM isolation requires root and util-linux unshare.')
        if native:
            missing = [name for name in ['mount', 'php8.4', 'mawk', 'sponge'] if shutil.which(name) is None]
            if missing:
                raise RuntimeError('Native VM test dependencies are missing: ' + ', '.join(missing))
        if not native and not (ROOTFS / 'usr/bin/proot').is_file():
            raise RuntimeError('Set DE_GWD_ROOTFS to a prepared PHP 7.4/PRoot rootfs; see tests/README.md.')
        cls.temp = tempfile.TemporaryDirectory(prefix='de-gwd-regression-', dir='/workspace')
        cls.fixture = Path(cls.temp.name)
        for directory in ['opt', 'bin', 'tmp/sessions', 'etc/nginx/conf.d', 'etc/wireguard', 'restore', 'var',
                          'mnt/data[1]', 'mnt/data1', 'mnt/keep']:
            (cls.fixture / directory).mkdir(parents=True)
        cls.configpath = cls.fixture / 'opt/0conf'
        cls.commands = cls.fixture / 'commands.jsonl'
        cls.commands.touch()
        cls.environment = dict(os.environ, PATH='/usr/local/bin:/usr/bin:/bin',
                               LC_ALL='C', LANG='C', GWD_TEST_COMMAND_LOG=str(cls.commands),
                               GWD_TEST_PASSWORD=PASSWORD)
        logger = '''#!/bin/bash
/usr/bin/php8.4 -n -r 'file_put_contents(getenv("GWD_TEST_COMMAND_LOG"), json_encode(array_slice($argv, 1))."\\n", FILE_APPEND | LOCK_EX);' -- "$@"
case "${1:-}" in
    curl)
        [[ -f /opt/de_GWD/fail-download ]] && exit 22
        printf '#!/bin/bash\\necho fixture\\n' > /opt/de_GWD/update.download ;;
    mv) shift; /bin/mv "$@" ;;
    showmount) printf 'Exports\\n/share\\n' ;;
    /opt/de_GWD/ui-checkDDNS3322|/opt/de_GWD/ui-checkDDNScf|/opt/de_GWD/ui-checkWG) echo success ;;
    cat) shift; /bin/cat "$@" ;;
    *) exit 0 ;;
esac
'''
        for name in ['sudo', 'systemctl']:
            (cls.fixture / 'bin' / name).write_text(logger)
            (cls.fixture / 'bin' / name).chmod(0o755)
        # Extracted Debian packages do not run update-alternatives; use the real mawk binary.
        (cls.fixture/'bin/awk').write_text('#!/bin/sh\nexec /usr/bin/mawk "$@"\n')
        (cls.fixture/'bin/awk').chmod(0o755)
        for name in ['ui-pingTCP', 'ui-speedT', 'ui-checkNodeNF', 'ui-checkNodeCU', 'ui-checkNodeDT', 'ui-checkNodeSS']:
            (cls.fixture / 'opt' / name).write_text('#!/bin/bash\nprintf "%s\\n" "${1:-}"\n')
            (cls.fixture / 'opt' / name).chmod(0o755)
        cls.ini = cls.fixture / 'php.ini'
        cls.ini.write_text('extension=gd.so\noutput_buffering=4096\n'
                           'session.save_path=/tmp/sessions\ndisplay_errors=Off\nlog_errors=On\n'
                           'error_reporting=E_ALL\ndate.timezone=Asia/Shanghai\n')
        bindings = [(cls.fixture/'tmp', Path('/tmp')),
                    (cls.fixture/'opt', Path('/opt/de_GWD')),
                    (cls.fixture/'bin', Path('/usr/local/bin')),
                    (cls.fixture/'etc', Path('/etc')),
                    (cls.fixture/'var', Path('/var')),
                    (cls.fixture/'mnt', Path('/mnt')),
                    (cls.fixture/'restore', WEB/'restore')]
        cls.created_mountpoints = []
        if native:
            mount = shutil.which('mount')
            if mount is None:
                raise RuntimeError('Native VM isolation requires mount from util-linux.')
            for _, target in bindings:
                if not target.exists():
                    target.mkdir(parents=True)
                    cls.created_mountpoints.append(target)
            wrapper = cls.fixture/'mount-runner.sh'
            wrapper.write_text('#!/bin/bash\nset -euo pipefail\n' +
                               shlex.quote(mount) + ' --make-rprivate /\n' +
                               ''.join(shlex.quote(mount) + ' --bind ' + shlex.quote(str(source)) +
                                       ' ' + shlex.quote(str(target)) + '\n'
                                       for source, target in bindings) +
                               'cd ' + shlex.quote(str(WEB)) + '\n' +
                               'exec "$@"\n')
            wrapper.chmod(0o755)
            cls.runner = [shutil.which('unshare'), '--mount',
                          '--propagation', 'private', str(wrapper)]
        else:
            triplet = {'x86_64': 'x86_64-linux-gnu',
                       'aarch64': 'aarch64-linux-gnu'}.get(platform.machine())
            if triplet is None:
                raise RuntimeError('Unsupported PRoot host architecture: ' + platform.machine())
            loader = ROOTFS / 'lib' / triplet / 'ld-2.31.so'
            if not loader.is_file():
                raise RuntimeError('Missing Debian 11 dynamic loader: ' + str(loader))
            proot = [str(loader), '--library-path',
                     str(ROOTFS / 'lib' / triplet) + ':' + str(ROOTFS / 'usr/lib' / triplet),
                     str(ROOTFS / 'usr/bin/proot')]
            cls.runner = proot + ['-r', str(ROOTFS), '-b', '/workspace', '-b', '/proc', '-b', '/dev']
            for source, target in bindings:
                cls.runner += ['-b', str(source) + ':' + str(target)]
            cls.runner += ['-w', str(WEB)]
        cls.php = cls.runner + ['/usr/bin/php8.4', '-c', str(cls.ini)]
        cls.router = cls.fixture / 'router.php'
        cls.router.write_text('<?php if (isset($_SERVER["HTTP_X_TEST_HTTPS"])) { $_SERVER["HTTPS"] = "on"; } return false;')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            cls.port = sock.getsockname()[1]
        cls.url = f'http://127.0.0.1:{cls.port}'
        cls.log = (cls.fixture/'server.log').open('w+')
        cls.server = subprocess.Popen(cls.php + ['-S', f'127.0.0.1:{cls.port}', '-t', str(WEB), str(cls.router)],
                                      env=cls.environment, stdout=cls.log, stderr=subprocess.STDOUT,
                                      start_new_session=True)
        cls.addClassCleanup(cls.cleanup)
        for _ in range(80):
            if cls.server.poll() is not None:
                cls.log.flush()
                cls.log.seek(0)
                raise RuntimeError('Isolated PHP server failed to start:\n' + cls.log.read())
            try:
                urllib.request.urlopen(cls.url + '/login.php', timeout=1).close()
                return
            except (OSError, urllib.error.URLError):
                time.sleep(0.1)
        raise RuntimeError('Isolated PHP server did not become ready.')

    @classmethod
    def cleanup(cls):
        if cls.server.poll() is None:
            os.killpg(cls.server.pid, signal.SIGTERM)
            cls.server.wait(timeout=10)
        cls.log.close()
        cls.temp.cleanup()
        for path in reversed(cls.created_mountpoints):
            path.rmdir()

    def setUp(self):
        self.config = {
            'address': {'PWD': PASSWORD_HASH, 'alias': 'Fixture', 'localIP': '127.0.0.1',
                        'upstreamIP': '', 'dhcpStart': '', 'dhcpEnd': ''},
            'dns': {'xDNS': '', 'hosts': {}}, 'app': {'bitwarden': ''},
            'v2node': [{'domain': 'development.test', 'name': 'Fixture node', 'tls': 'on', 'uuid': '', 'path': '/fixture'}],
            'wireguard': {'clients': [{}]},
            'v2nodeDIV': {'nodeDT': {'display': 'off', 'ip': []}, 'nodeCU': {'display': 'off', 'rules': {}},
                          'nodeNF': {'display': 'off'}, 'ss': {'ssAddress': '', 'ssPort': ''}},
            'FORWARD': {'block53': 'off', 'domain': '', 'APIkey': '', 'Email': '',
                        'FWD0': {'port': '', 'uuid': []}, 'FWD1': {'port': '', 'uuid': []}, 'xDNSs': {'port': ''},
                        'Rproxy': {'server': {'inStatus': 'off', 'inUUID': [], 'mapping': [],
                                              'tunnel': {'port': '', 'uuid': ''}},
                                   'client': {'tunnel': {'address': '', 'uuid': ''}, 'mapping': []}}},
            'ddns': {'ddns3322': {'domain': '', 'user': '', 'pwd': ''},
                     'ddnsCF': {'cfDomain': '', 'cfAPIkey': '', 'cfEmail': ''}},
            'update': {'updateAddr': '', 'updatePort': '', 'updateCMD': ''},
            'listB': {}, 'listW': {}, 'listBlan': [], 'listWlan': [],
        }
        self.save_config()
        (self.fixture/'opt/fail-download').unlink(missing_ok=True)
        for name, value in [('vtrui', {'inbounds': [], 'outbounds': []}),
                            ('v2dns', {}), ('RproxyS', None), ('RproxyC', None)]:
            path = self.fixture/'opt'/name
            path.mkdir(exist_ok=True)
            (path/'config.json').write_text(json.dumps(value))
        (self.fixture/'opt/iptables-proxy-up').write_text('')
        for name in ['default.conf', 'filerun.conf']:
            (self.fixture/'etc/nginx/conf.d'/name).write_text('server { listen 8080; server_name de_GWD; }')
        (self.fixture/'etc/smartdns').mkdir(exist_ok=True)
        (self.fixture/'etc/smartdns/smartdns.conf').write_text('')
        self.jar = http.cookiejar.CookieJar()
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), NoRedirect())
        self.command_offset = self.commands.stat().st_size
        self.log.seek(0, 2)
        self.log_offset = self.log.tell()

    def save_config(self):
        self.configpath.write_text(json.dumps(self.config))

    def request(self, path, data=None, headers=None, client=None):
        if isinstance(data, dict):
            data = urllib.parse.urlencode(data).encode()
        request = urllib.request.Request(self.url + path, data=data, headers=headers or {})
        try:
            response = (client or self.client).open(request, timeout=10)
        except urllib.error.HTTPError as response:
            return response.code, response.headers, response.read().decode()
        with response:
            return response.status, response.headers, response.read().decode()

    def login(self, headers=None):
        self.assertEqual(self.request('/auth.php', {'gwdpw': PASSWORD}, headers)[0], 200)
        self.assertEqual(self.request('/act/uptime.php')[0], 200)

    def command_events(self):
        with self.commands.open() as log:
            log.seek(self.command_offset)
            return [json.loads(line) for line in log.read().splitlines() if line]

    def wait_for_command(self, name):
        for _ in range(100):
            if any(event and event[0] == name for event in self.command_events()):
                return
            time.sleep(0.02)
        self.fail('Expected command did not execute: ' + name)

    def test_all_action_endpoints_require_authentication(self):
        before = self.commands.read_bytes()
        for file in sorted((WEB/'act').glob('*.php')):
            with self.subTest(endpoint=file.name):
                self.assertEqual(self.request('/act/' + file.name)[0], 403)
        self.assertEqual(self.commands.read_bytes(), before)

    def test_forged_and_password_hash_cookies_are_rejected(self):
        for cookie in ['forged', PASSWORD_HASH, '0', '', '1:' + '0'*64]:
            with self.subTest(cookie_type=len(cookie)):
                anonymous = urllib.request.build_opener(NoRedirect())
                self.assertEqual(self.request('/act/uptime.php', headers={'Cookie': 'persistentlogin=' + cookie},
                                              client=anonymous)[0], 403)

    def test_forged_cookie_cannot_open_dashboard(self):
        status, headers, _ = self.request('/index.php', headers={'Cookie': 'persistentlogin=forged'},
                                          client=urllib.request.build_opener(NoRedirect()))
        self.assertEqual(status, 302)
        self.assertEqual(headers['Location'], 'login.php')

    def test_login_rotates_session_and_cookie_has_scope_and_flags(self):
        before = 'fixationfixture'
        status, headers, _ = self.request('/auth.php', {'gwdpw': PASSWORD},
                                         {'Cookie': 'PHPSESSID=' + before})
        self.assertEqual(status, 200)
        after = next(cookie.value for cookie in self.jar if cookie.name == 'PHPSESSID')
        self.assertNotEqual(before, after)
        cookie = next(cookie for cookie in self.jar if cookie.name == 'persistentlogin')
        self.assertNotEqual(cookie.value, PASSWORD_HASH)
        self.assertEqual(cookie.path, '/')
        self.assertTrue(cookie.has_nonstandard_attr('HttpOnly'))
        self.assertEqual(cookie.get_nonstandard_attr('SameSite'), 'Strict')
        self.assertFalse(cookie.secure)
        self.assertEqual(self.request('/act/uptime.php')[0], 200)
        _, secure_headers, _ = self.request('/auth.php', {'gwdpw': PASSWORD}, {'X-Test-Https': '1'})
        self.assertTrue(any('secure' in value.lower() for value in secure_headers.get_all('Set-Cookie')))

    def test_persistent_login_expiry_tampering_and_password_change(self):
        self.login()
        token = urllib.parse.unquote(next(cookie.value for cookie in self.jar if cookie.name == 'persistentlogin'))
        anonymous = urllib.request.build_opener(NoRedirect())
        self.assertEqual(self.request('/act/uptime.php', headers={'Cookie': 'persistentlogin=' + token},
                                      client=anonymous)[0], 200)
        for expiry in [str(int(time.time())-60), str(int(time.time())+8*86400)]:
            signature = hmac.new(PASSWORD_HASH.encode(), ('persistentlogin:' + expiry).encode(), hashlib.sha256).hexdigest()
            self.assertEqual(self.request('/act/uptime.php', headers={'Cookie': 'persistentlogin=' + expiry + ':' + signature},
                                          client=urllib.request.build_opener(NoRedirect()))[0], 403)
        self.config['address']['PWD'] = '0'*64
        self.save_config()
        self.assertEqual(self.request('/act/uptime.php')[0], 403)

    def test_wrong_password_then_correct_password_and_logout(self):
        self.request('/auth.php', {'gwdpw': 'wrong'})
        self.assertEqual(self.request('/act/uptime.php')[0], 403)
        self.login()
        self.request('/auth.php', {'gwdpw': 'wrong'}, {'Cookie': 'persistentlogin=forged'})
        self.login()
        self.request('/auth.php?logout=1')
        self.assertFalse(any(cookie.name in ['persistentlogin', 'PHPSESSID'] for cookie in self.jar))
        self.assertEqual(self.request('/act/uptime.php')[0], 403)

    def test_missing_invalid_config_and_array_parameters_fail_closed(self):
        for config in [{}, {'address': {'PWD': ''}}, {'address': {'PWD': []}}]:
            self.configpath.write_text(json.dumps(config))
            self.assertEqual(self.request('/act/uptime.php', headers={'Cookie': 'persistentlogin=forged'})[0], 403)
        self.configpath.write_text('{bad json')
        self.assertEqual(self.request('/act/uptime.php')[0], 403)
        self.configpath.write_text(json.dumps({'address': {'PWD': '0'*64}}))
        expiry = str(int(time.time()) + 60)
        signature = hmac.new(('0'*64).encode(), ('persistentlogin:' + expiry).encode(), hashlib.sha256).hexdigest()
        self.assertEqual(self.request('/act/uptime.php', headers={'Cookie': 'persistentlogin=' + expiry + ':' + signature})[0], 403)
        self.save_config()
        self.request('/auth.php', b'gwdpw[]=bad')
        self.assertEqual(self.request('/act/uptime.php')[0], 403)

    def test_index_parameters_cannot_execute_shell_code(self):
        self.login()
        endpoints = {'changeNode.php': 'nodenum', 'changeNodeCU.php': 'nodeCUnum',
                     'changeNodeDT.php': 'nodeDTnum', 'changeNodeNF.php': 'nodeNFnum',
                     'WGgenCkey.php': 'WGnum', 'WGqr.php': 'WGnum',
                     'pingTCP.php': 'pingTCP', 'speedT.php': 'speedT'}
        marker = self.fixture/'injected'
        before = self.commands.read_bytes()
        for endpoint, parameter in endpoints.items():
            for value in ['0;touch ' + str(marker), '-1', '1.5', 'x', '9999999999']:
                with self.subTest(endpoint=endpoint, value=value):
                    query = urllib.parse.urlencode({parameter: value})
                    self.assertEqual(self.request('/act/' + endpoint + '?' + query)[0], 400)
            self.assertEqual(self.request('/act/' + endpoint + '?' + parameter + '[]=0')[0], 400)
        self.assertFalse(marker.exists())
        self.assertEqual(self.commands.read_bytes(), before)
        self.assertEqual(self.request('/act/pingTCP.php?pingTCP=00')[2].strip(), '0')
        self.assertEqual(self.request('/act/pingTCP.php?pingTCP=1')[0], 400)
        self.assertEqual(self.request('/act/WGgenCkey.php?WGnum=2')[0], 400)
        self.assertEqual(self.request('/act/WGqr.php?WGnum=0')[0], 400)
        (self.fixture/'etc/wireguard/client1.conf').write_text('client fixture')
        self.assertEqual(self.request('/act/WGqr.php?WGnum=1')[2].strip(), 'client fixture')

    def test_nfs_arguments_are_literal_and_hosts_are_validated(self):
        self.login()
        mount = '/mnt/share;touch ' + str(self.fixture/'injected')
        status, _, _ = self.request('/act/offNFS.php?' + urllib.parse.urlencode({'NFSpoint': mount}))
        self.assertEqual(status, 200)
        self.wait_for_command('/opt/de_GWD/ui-offNFS')
        self.assertIn(['/opt/de_GWD/ui-offNFS', mount], self.command_events())
        self.assertFalse((self.fixture/'injected').exists())
        self.assertEqual(self.request('/act/getNFSlist.php?NFSserver=host%3Btouch+x')[0], 400)
        self.assertEqual(self.request('/act/getNFSlist.php?NFSserver=-e')[0], 400)
        self.assertEqual(self.request('/act/getNFSlist.php?NFSserver=files.example.test')[2].strip(), '/share')

    def test_nfs_unmount_removes_only_the_exact_configured_path(self):
        auto = self.fixture/'etc/auto.nfs'
        auto.write_text('/mnt/data[1] -rw host:/first\n/mnt/data1 -rw host:/second\n/mnt/keep -rw host:/keep\n')
        result = subprocess.run(self.runner + ['/bin/bash', str(REPO/'resource/client/ui-script/ui-offNFS'),
                                               '/mnt/data[1]'], env=self.environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(auto.read_text(), '/mnt/data1 -rw host:/second\n/mnt/keep -rw host:/keep\n')

    def test_filerun_download_failure_returns_before_system_writes(self):
        (self.fixture/'etc/nginx/conf.d/filerun.conf').unlink()
        source = (REPO/'resource/client/ui-script/ui-installFileRun').read_text().rsplit('\nstart_menu', 1)[0]
        function_file = self.fixture/'filerun-functions.sh'
        function_file.write_text(source)
        script = '''
clear() { :; }
dpkg() { if [[ "$1" == --print-architecture ]]; then echo amd64; else echo ' mariadb-server '; fi; }
apt() { :; }
wget() { :; }
rm() { :; }
unzip() { echo 'Corrupt archive'; return 1; }
source "$1"
installFileRun
result=$?
[[ $result == 1 ]]
'''
        result = subprocess.run(self.runner + ['/bin/bash', '-c', script, 'fixture', str(function_file)],
                                env=self.environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.fixture/'etc/nginx/conf.d/filerun.conf').exists())

    def test_installers_stop_after_apt_failure(self):
        (self.fixture/'etc/apt/apt.conf.d').mkdir(parents=True, exist_ok=True)
        # Reaching profile means dependency failure was ignored. Exit before
        # any later installer operations could escape the fixture.
        (self.fixture/'etc/profile').write_text('exit 97\n')
        for installer, failures in [('server', ['update', 'upgrade', 'install']),
                                    ('client', ['update', 'install'])]:
            body = (REPO/installer).read_text().split('preInstall(){\n', 1)[1].split('\n}\n', 1)[0]
            functions = self.fixture/'dependency-functions.sh'
            functions.write_text('preInstall(){\n' + body + '\n}\n')
            for failure in failures:
                with self.subTest(installer=installer, failed_phase=failure):
                    script = '''
virtVC=container
DEBIAN_SOURCES_origin=fixture
dpkg() { return 0; }
rm() { :; }
free() { printf 'header\\nMemory: 1\\nSwap: 1\\n'; }
apt() { [[ $1 != "$failed_phase" ]]; }
source "$1"
preInstall
'''
                    environment = dict(self.environment, failed_phase=failure)
                    result = subprocess.run(self.runner + ['/bin/bash', '-c', script, 'fixture', str(functions)],
                                            env=environment, capture_output=True, text=True, timeout=20)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_client_preparation_failures_keep_gateway_and_dns_untouched(self):
        text=(REPO/'client').read_text()
        functions=self.fixture/'client-download-functions.sh'
        functions.write_text('\n'.join(start+'\n'+text.split(start+'\n',1)[1].split('\n}\n',1)[0]+'\n}'
                                      for start in ('preDownload() {','updateGWD(){'))+'\n')
        valid=self.fixture/'tmp/core-valid.zip'
        with zipfile.ZipFile(valid,'w') as archive:
            for name in ('xray','v2ray','geoip.dat','geosite.dat'):archive.writestr(name,b'fixture')
        incomplete=self.fixture/'tmp/core-incomplete.zip'
        with zipfile.ZipFile(incomplete,'w') as archive:archive.writestr('xray',b'fixture')
        dns=self.fixture/'etc/dnsmasq.conf';dns.write_text('existing DNS configuration\n')
        before=self.configpath.read_bytes()
        for failure in ('download','archive','incomplete','helper','rules'):
            with self.subTest(failure=failure):
                marker=self.fixture/'opt/cleanup-reached';marker.unlink(missing_ok=True)
                script='''
source "$1"
architecture=arm64
sourceDirectory=/tmp/checkout-not-present
requireDebian13() { return 0; }
preUpdate() { return 0; }
cleanDep() { touch /opt/de_GWD/cleanup-reached; return 0; }
copyOrDownloadAsset() {
    if [[ $1 == *.zip ]]; then
        case "$failed_phase" in
            download) return 22;;
            archive) printf 'broken zip' >"$2";;
            incomplete) cp /tmp/core-incomplete.zip "$2";;
            *) cp /tmp/core-valid.zip "$2";;
        esac
    else
        [[ $failed_phase != helper ]] || return 22
        printf '#!/bin/bash\\nexit 0\\n' >"$2"
    fi
}
installRulesUpdater() {
    printf '#!/bin/bash\\nexit 1\\n' >/opt/de_GWD/ui-updateRules
    chmod +x /opt/de_GWD/ui-updateRules
}
updateGWD
'''
                offset=self.commands.stat().st_size
                result=subprocess.run(self.runner+['/bin/bash','-c',script,'fixture',str(functions)],
                                      input='N',env=dict(self.environment,failed_phase=failure),
                                      text=True,capture_output=True,timeout=20)
                self.assertEqual(result.returncode,1,result.stdout+result.stderr)
                self.assertFalse(marker.exists(),'Cleanup ran before downloads were ready')
                self.assertEqual(self.configpath.read_bytes(),before)
                self.assertEqual(dns.read_text(),'existing DNS configuration\n')
                self.assertEqual(self.commands.stat().st_size,offset,'A service was stopped during failed preparation')

    def test_client_time_check_does_not_fetch_http_or_change_clock(self):
        text=(REPO/'client').read_text()
        body=text.split('cat << "EOF" >/opt/de_GWD/tcpTime\n',1)[1].split('\nEOF',1)[0]
        script=self.fixture/'time-check.sh';script.write_text(body+'\n')
        fake=self.fixture/'bin/timedatectl'
        fake.write_text('#!/bin/sh\nprintf "%s\\n" "$GWD_TEST_NTP"\nexit "$GWD_TEST_NTP_EXIT"\n');fake.chmod(0o755)
        for name in ('wget','date','hwclock'):
            path=self.fixture/'bin'/name
            path.write_text('#!/bin/sh\ntouch /opt/de_GWD/unsafe-clock-request\nexit 99\n');path.chmod(0o755)
        marker=self.fixture/'opt/unsafe-clock-request';marker.unlink(missing_ok=True)
        for value,code in (('yes','0'),('no','0'),('','1')):
            with self.subTest(ntp=value,exit=code):
                r=subprocess.run(self.runner+['/bin/bash',str(script)],text=True,capture_output=True,
                    env=dict(self.environment,GWD_TEST_NTP=value,GWD_TEST_NTP_EXIT=code),timeout=10)
                self.assertEqual(r.returncode,0,r.stderr)
                self.assertFalse(marker.exists(),'The time check fetched HTTP or changed the clock')
        for name in ('wget','date','hwclock','timedatectl'):(self.fixture/'bin'/name).unlink()

    def test_update_download_is_ordered_and_keeps_complete_url(self):
        self.login()
        query = {'updateAddr': '127.0.0.1', 'updatePort': '3000',
                 'updateCMD': 'bash <(wget -qO- https://updates.example.test/client)'}
        self.assertEqual(self.request('/act/updateSave.php?' + urllib.parse.urlencode(query))[2].strip(), '127.0.0.1:3000')
        self.wait_for_command('/opt/de_GWD/ui-updateSave')
        events = self.command_events()
        self.assertIn('https://updates.example.test/client', next(event for event in events if event[0]=='curl'))
        self.assertTrue((self.fixture/'opt/update').exists())
        self.assertLess(next(i for i,e in enumerate(events) if e[0]=='curl'),
                        next(i for i,e in enumerate(events) if e[0]=='/opt/de_GWD/ui-updateSave'))
        query['updateCMD'] = 'https://updates.example.test/script'
        self.assertEqual(self.request('/act/updateSave.php?' + urllib.parse.urlencode(query))[0], 200)
        self.assertIn('https://updates.example.test/script', [e for e in self.command_events() if e[0]=='curl'][-1])

    def test_failed_update_does_not_replace_config_or_existing_script(self):
        self.login()
        before = self.configpath.read_bytes()
        script = self.fixture/'opt/update'
        script.write_text('existing script')
        (self.fixture/'opt/fail-download').touch()
        query = urllib.parse.urlencode({'updateAddr': '127.0.0.1', 'updatePort': '3000',
                                        'updateCMD': 'https://updates.example.test/script'})
        self.assertEqual(self.request('/act/updateSave.php?' + query)[0], 502)
        self.assertEqual(self.configpath.read_bytes(), before)
        self.assertEqual(script.read_text(), 'existing script')
        self.assertEqual(self.request('/act/updateSave.php?updateAddr=127.0.0.1&updatePort=0&updateCMD=x')[0], 400)

    def test_invalid_upload_does_not_delete_backup_or_start_restore(self):
        self.login()
        for endpoint, filename in [('restore.php', 'de_GWD_bak'), ('restoreBitwarden.php', 'Bitwarden_bak.zip')]:
            backup = self.fixture/'restore'/filename
            backup.write_text('existing backup')
            before = self.commands.read_bytes()
            self.assertEqual(self.request('/act/' + endpoint, b'')[0], 400)
            self.assertEqual(backup.read_text(), 'existing backup')
            self.assertEqual(self.commands.read_bytes(), before)
            boundary = 'regression-upload-boundary'
            body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                    'Content-Type: application/octet-stream\r\n\r\nfixture backup\r\n'
                    f'--{boundary}--\r\n').encode()
            self.assertEqual(self.request('/act/' + endpoint, body,
                                          {'Content-Type': 'multipart/form-data; boundary=' + boundary})[0], 200)
            self.assertEqual(backup.read_text(), 'fixture backup')
            self.wait_for_command('/opt/de_GWD/ui-restore' if endpoint == 'restore.php'
                                  else '/opt/de_GWD/ui-restoreBitwarden')

    def test_ddns_state_checks_return_output(self):
        self.login()
        for endpoint in ['checkDDNS3322.php', 'checkDDNScf.php']:
            self.assertEqual(self.request('/act/' + endpoint)[2].strip(), 'installed')

    def test_uptime_boundaries(self):
        code = ('$_GET["gwdpw"]=getenv("GWD_TEST_PASSWORD"); ob_start(); require '
                + json.dumps(str(WEB/'act/uptime.php')) + '; ob_end_clean(); '
                'echo json_encode(array_map("gwdFormatUptime", [0,59,60,3599,3600,86400,90061]));')
        result = subprocess.run(self.php + ['-r', code], env=self.environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ['0分钟','0分钟','1分钟','59分钟','1小时0分钟','1天0分钟','1天1小时1分钟'])

    def test_authenticated_pages_have_no_php_warnings(self):
        self.login()
        for page in ['index.php','forward.php','ddns.php','app.php','listBW.php','nodeMAN.php','update.php','routing.php']:
            with self.subTest(page=page):
                status, _, body = self.request('/' + page)
                self.assertEqual(status, 200)
                self.assertIn('</html>', body)
        self.log.flush()
        self.log.seek(self.log_offset)
        recent = self.log.read()
        self.assertNotIn('PHP Warning:', recent)
        self.assertNotIn('PHP Fatal error:', recent)


class DistributionTests(unittest.TestCase):
    def test_installer_archive_matches_current_sources(self):
        root = REPO/'resource/client'
        expected = {path.relative_to(root).as_posix(): path for directory in ['ui-script','ui-web']
                    for path in (root/directory).rglob('*') if path.is_file()}
        with zipfile.ZipFile(root/'Archive.zip') as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(set(archive.namelist()), set(expected))
            for name, path in expected.items():
                with self.subTest(file=name):
                    self.assertEqual(archive.read(name), path.read_bytes())


if __name__ == '__main__':
    unittest.main(verbosity=2)
