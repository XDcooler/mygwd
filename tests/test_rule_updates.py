import hashlib
import fcntl
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch
import zipfile

REPO = Path(__file__).resolve().parents[1]
helper = REPO/'resource/client/ui-script/ui-updateRules'
body = helper.read_text().split("<<'PY_RULE_UPDATE'\n", 1)[1].rsplit('\nPY_RULE_UPDATE', 1)[0]
rules = types.ModuleType('rule_updater')
exec(compile(body, str(helper), 'exec'), rules.__dict__)


def encode_varint(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def field(number, value):
    return encode_varint((number << 3) | 2) + encode_varint(len(value)) + value


def geodata(names):
    return b''.join(field(1, field(1, name.encode())) for name in names)


class RuleUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rule-update-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.destination = self.root/'rules'
        self.destination.mkdir()
        (self.destination/'geoip.dat').write_bytes(b'previous geoip')
        (self.destination/'manifest.json').write_bytes(b'previous manifest')
        self.previous = {p.name: p.read_bytes() for p in self.destination.iterdir()}
        self.payloads = {
            'geoip.dat': geodata(['CN','PRIVATE']),
            'geosite.dat': geodata(['CN','PRIVATE','APPLE','APPLE-CN','NETFLIX','CATEGORY-ADS-ALL']),
            'chnroutes.txt': b'# source fixture\n1.1.8.0/24\n1.2.4.0/24\n1.1.8.0/24\n',
            'hosts': b'# fixture\n0.0.0.0 ads.example\n0.0.0.0 shared.example\n127.0.0.1 localhost.localdomain\n0.0.0.0 0.0.0.0\n',
            'anti-ad-domains.txt': b'# fixture\ncn.example\nshared.example\n',
        }
        for name in ['geoip.dat','geosite.dat']:
            self.payloads[name+'.sha256sum'] = (hashlib.sha256(self.payloads[name]).hexdigest() + '  ' + name + '\n').encode()

    def fetch_fixture(self, url, destination):
        destination.write_bytes(self.payloads[url.rsplit('/',1)[1]])

    def test_https_commit_resolution_avoids_stalled_git_endpoint(self):
        with patch.object(rules.subprocess, 'run', return_value=types.SimpleNamespace(
                stdout=json.dumps({'sha': 'a'*40}))) as run:
            self.assertEqual(rules.resolve_source('owner/repository', 'release'), 'a'*40)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][0], 'curl')
            self.assertNotIn('--insecure', run.call_args.args[0])

    def test_api_failure_falls_back_and_rejects_invalid_commit(self):
        for output in ('not JSON', '{"sha":"not-a-commit"}'):
            with self.subTest(output=output), patch.object(rules.subprocess, 'run', side_effect=[
                    types.SimpleNamespace(stdout=output),
                    types.SimpleNamespace(stdout='b'*40+'\trefs/heads/release\n')]):
                self.assertEqual(rules.resolve_source('owner/repository', 'release'), 'b'*40)
        with patch.object(rules.subprocess, 'run', side_effect=[
                subprocess.TimeoutExpired('curl',25), types.SimpleNamespace(stdout='invalid')]):
            with self.assertRaisesRegex(ValueError, 'Unable to resolve'):
                rules.resolve_source('owner/repository', 'release')

    def test_bootstrap_routes_use_verified_snapshot_without_network(self):
        self.update()
        installed = self.root/'installed'
        with patch.object(rules, 'resolve_source', side_effect=AssertionError('network called')), \
                patch.object(rules, 'refresh_ipset') as refresh:
            rules.bootstrap_routes(self.destination, installed)
        self.assertEqual((installed/'chnrouteSET').read_text(),
                         'add chnroute 1.1.8.0/24\nadd chnroute 1.2.4.0/24\n')
        refresh.assert_called_once_with(installed/'chnrouteSET')
        self.assertFalse((installed/'rule-update-status.json').exists())

    def test_bootstrap_rejects_bad_snapshot_and_restores_failed_application(self):
        self.update()
        installed=self.root/'installed';installed.mkdir()
        target=installed/'chnrouteSET';target.write_text('previous routes');target.chmod(0o640)
        with patch.object(rules, 'refresh_ipset', side_effect=ValueError('ipset failed')):
            with self.assertRaisesRegex(ValueError,'ipset failed'):
                rules.bootstrap_routes(self.destination, installed)
        self.assertEqual(target.read_text(),'previous routes')
        self.assertEqual(target.stat().st_mode & 0o777,0o640)
        (self.destination/'chnroutes.txt').write_text('modified unverified routes')
        with self.assertRaisesRegex(ValueError,'checksum mismatch'):
            rules.bootstrap_routes(self.destination, installed)
        self.assertEqual(target.read_text(),'previous routes')

    def update(self, downloader=None, routes_only=False):
        with patch.object(rules, 'resolve_source', return_value='a'*40), \
                patch.object(rules, 'download', side_effect=downloader or self.fetch_fixture):
            return rules.update_rules(self.destination, self.root/'installed', self.root/'pihole',
                                      routes_only=routes_only, data_only=True)

    def assert_previous_unchanged(self):
        self.assertEqual({p.name:p.read_bytes() for p in self.destination.iterdir()}, self.previous)

    def test_checksum_failure_keeps_existing_data(self):
        self.payloads['geosite.dat.sha256sum'] = ('0'*64 + '  geosite.dat\n').encode()
        with self.assertRaisesRegex(ValueError, 'Checksum mismatch'):
            self.update()
        self.assert_previous_unchanged()

    def test_late_network_failure_keeps_existing_data(self):
        def failing(url, destination):
            if url.endswith('/chnroutes.txt'):
                raise subprocess.CalledProcessError(22, ['curl'])
            self.fetch_fixture(url, destination)
        with self.assertRaises(subprocess.CalledProcessError):
            self.update(failing)
        self.assert_previous_unchanged()

    def test_invalid_routes_and_advertising_data_are_rejected(self):
        for name, payload in [('chnroutes.txt', b'<html>failure</html>'),
                              ('anti-ad-domains.txt', b'<html>failure</html>')]:
            with self.subTest(source=name):
                original = self.payloads[name]
                self.payloads[name] = payload
                with self.assertRaises(ValueError):
                    self.update()
                self.assert_previous_unchanged()
                self.payloads[name] = original
        with self.assertRaises(ValueError):
            rules.parse_routes('2001:db8::/32')

    def test_successful_snapshot_has_valid_hashes_and_deduplicated_rules(self):
        manifest = self.update()
        self.assertEqual(manifest['counts']['china_ipv4_routes'], 2)
        self.assertEqual(manifest['counts']['advertising_domains'], 3)
        self.assertEqual((self.destination/'ads-domains.txt').read_text(), 'ads.example\ncn.example\nshared.example\n')
        self.assertEqual((self.destination/'chnrouteSET').read_text(),
                         'add chnroute 1.1.8.0/24\nadd chnroute 1.2.4.0/24\n')
        for filename, metadata in manifest['files'].items():
            self.assertEqual(hashlib.sha256((self.destination/filename).read_bytes()).hexdigest(), metadata['sha256'])
        self.assertTrue(all(source['commit']=='a'*40 for source in manifest['sources'].values()))

    def test_routes_only_does_not_replace_geodata(self):
        manifest = self.update(routes_only=True)
        self.assertEqual((self.destination/'geoip.dat').read_bytes(), b'previous geoip')
        self.assertEqual(set(manifest['sources']), {'routes'})
        self.assertFalse((self.destination/'ads-domains.txt').exists())

    def test_application_failure_restores_snapshot_and_private_configuration(self):
        installed = self.root/'installed'
        (installed/'vtrui').mkdir(parents=True)
        private = installed/'0conf'
        private.write_text('private previous configuration')
        private.chmod(0o640)
        (installed/'vtrui/geosite.dat').write_bytes(b'previous live categories')
        def failing(directory, root, pihole, routes_only):
            rules.atomic_copy(directory/'geosite.dat', root/'vtrui/geosite.dat')
            private.write_text('partially applied configuration')
            raise ValueError('core rejected new rules')
        with patch.object(rules, 'resolve_source', return_value='a'*40), \
                patch.object(rules, 'download', side_effect=self.fetch_fixture), \
                patch.object(rules, 'deploy_rules', side_effect=failing):
            with self.assertRaisesRegex(ValueError, 'core rejected'):
                rules.update_rules(self.destination, installed, self.root/'pihole')
        self.assert_previous_unchanged()
        self.assertEqual(private.read_text(), 'private previous configuration')
        self.assertEqual(private.stat().st_mode & 0o777, 0o640)
        self.assertEqual((installed/'vtrui/geosite.dat').read_bytes(), b'previous live categories')

    def test_overlapping_update_is_rejected_without_replacing_data(self):
        lock = os.open(self.root, os.O_RDONLY)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.update()
            self.assert_previous_unchanged()
        finally:
            os.close(lock)

    def test_sqlite_registration_preserves_disabled_lists_groups_and_allowlist(self):
        directory = self.root/'pihole'
        directory.mkdir()
        with sqlite3.connect(directory/'gravity.db') as connection:
            connection.executescript('''
                CREATE TABLE adlist (id INTEGER PRIMARY KEY, address TEXT UNIQUE, enabled INTEGER, comment TEXT);
                CREATE TABLE adlist_by_group (adlist_id INTEGER, group_id INTEGER, PRIMARY KEY(adlist_id,group_id));
                CREATE TRIGGER tr_adlist_add AFTER INSERT ON adlist BEGIN
                    INSERT INTO adlist_by_group (adlist_id,group_id) VALUES (NEW.id,0);
                END;
                CREATE TABLE domainlist (type INTEGER, domain TEXT UNIQUE);
                INSERT INTO adlist VALUES (1,'https://custom.example/list',1,'user source');
                INSERT INTO domainlist VALUES (0,'allow.example');
            ''')
            connection.execute('INSERT INTO adlist VALUES (2,?,0,?)', (rules.ADLISTS[0],'user disabled'))
            connection.execute('UPDATE adlist_by_group SET group_id=2 WHERE adlist_id=2')
        rules.register_adlists(directory)
        rules.register_adlists(directory)
        with sqlite3.connect(directory/'gravity.db') as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM adlist').fetchone()[0], 3)
            self.assertEqual(connection.execute('SELECT enabled,comment FROM adlist WHERE id=2').fetchone(),
                             (0,'user disabled'))
            self.assertEqual(connection.execute('SELECT group_id FROM adlist_by_group WHERE adlist_id=2').fetchall(),[(2,)])
            self.assertEqual(connection.execute('SELECT * FROM domainlist').fetchall(),[(0,'allow.example')])
            new_id = connection.execute('SELECT id FROM adlist WHERE address=?',(rules.ADLISTS[1],)).fetchone()[0]
            self.assertEqual(connection.execute('SELECT group_id FROM adlist_by_group WHERE adlist_id=?',(new_id,)).fetchall(),[(0,)])

    def test_legacy_adlist_registration_is_additive_and_repeatable(self):
        directory = self.root/'pihole'
        directory.mkdir()
        path = directory/'adlists.list'
        path.write_text('# user lists\nhttps://custom.example/list')
        rules.register_adlists(directory)
        first = path.read_text()
        rules.register_adlists(directory)
        self.assertEqual(path.read_text(), first)
        self.assertTrue(first.startswith('# user lists\nhttps://custom.example/list\n'))
        for address in rules.ADLISTS:
            self.assertEqual(first.count(address), 1)

    def test_ipset_failure_does_not_swap_the_live_set(self):
        source = self.root/'chnrouteSET'
        source.write_text('add chnroute 1.1.8.0/24\n')
        calls = []
        def command(arguments, **kwargs):
            calls.append(arguments)
            if arguments[1]=='restore':
                raise subprocess.CalledProcessError(1,arguments)
            return subprocess.CompletedProcess(arguments,0)
        with patch.object(rules.shutil, 'which', return_value='/usr/sbin/ipset'), \
                patch.object(rules.subprocess, 'run', side_effect=command):
            with self.assertRaises(subprocess.CalledProcessError):
                rules.refresh_ipset(source)
        self.assertFalse(any(call[1]=='swap' for call in calls))
        self.assertEqual(calls[-1][1], 'destroy')

    def test_ipset_swap_removes_stale_routes(self):
        source = self.root/'chnrouteSET'
        source.write_text('add chnroute 1.1.8.0/24\n')
        sets = {'chnroute': {'old route'}}
        def command(arguments, **kwargs):
            action = arguments[1]
            if action=='create':
                sets.setdefault(arguments[2],set())
            elif action=='restore':
                for line in kwargs['input'].splitlines():
                    _, name, network = line.split()
                    sets[name].add(network)
            elif action=='swap':
                a,b = arguments[2:4]
                sets[a],sets[b] = sets[b],sets[a]
            elif action=='destroy':
                del sets[arguments[2]]
            return subprocess.CompletedProcess(arguments,0)
        with patch.object(rules.shutil,'which',return_value='/usr/sbin/ipset'), \
                patch.object(rules.subprocess,'run',side_effect=command):
            rules.refresh_ipset(source)
        self.assertEqual(sets,{'chnroute':{'1.1.8.0/24'}})

    def test_deploy_copies_data_to_installed_components_and_seeds_adlists(self):
        self.update()
        installed = self.root/'installed'
        for component in ['v2dns','vtrui']:
            (installed/component).mkdir(parents=True)
        with patch.object(rules.shutil,'which',return_value=None):
            rules.deploy_rules(self.destination,installed,self.root/'pihole')
        for component in ['v2dns','vtrui']:
            for filename in ['geoip.dat','geosite.dat']:
                self.assertEqual((installed/component/filename).read_bytes(), self.payloads[filename])
        self.assertEqual((installed/'chnrouteSET').read_bytes(),(self.destination/'chnrouteSET').read_bytes())
        for address in rules.ADLISTS:
            self.assertIn(address,(self.root/'pihole/adlists.list').read_text())

    def test_download_preserves_tls_verification(self):
        destination = self.root/'download'
        def command(arguments, **kwargs):
            destination.write_bytes(b'fixture')
            self.assertIn('--proto', arguments)
            self.assertIn('=https', arguments)
            self.assertNotIn('--insecure', arguments)
            return subprocess.CompletedProcess(arguments,0)
        with patch.object(rules.subprocess, 'run', side_effect=command):
            rules.download('https://example.test/rules', destination)


class PublishedRuleTests(unittest.TestCase):
    def test_snapshot_checksums_and_required_categories(self):
        directory = REPO/'resource/rules'
        manifest = json.loads((directory/'manifest.json').read_text())
        for filename, metadata in manifest['files'].items():
            self.assertEqual(hashlib.sha256((directory/filename).read_bytes()).hexdigest(), metadata['sha256'])
        rules.verify_geodata(directory/'geoip.dat', directory/'geoip.dat.sha256sum', {'CN','PRIVATE'})
        rules.verify_geodata(directory/'geosite.dat', directory/'geosite.dat.sha256sum', {'CN','CATEGORY-ADS-ALL'})
        self.assertEqual(len(rules.parse_routes((directory/'chnroutes.txt').read_text())),
                         manifest['counts']['china_ipv4_routes'])
        self.assertEqual(len(set((directory/'ads-domains.txt').read_text().splitlines())),
                         manifest['counts']['advertising_domains'])

    def test_both_architecture_packages_have_updated_data_and_unchanged_cores(self):
        directory = REPO/'resource/rules'
        for architecture in ['amd64','arm64']:
            path = 'resource/vtrui_' + architecture + '.zip'
            original = subprocess.run(['git','show','HEAD:'+path],cwd=REPO,capture_output=True,check=True).stdout
            with zipfile.ZipFile(io.BytesIO(original)) as baseline, zipfile.ZipFile(REPO/path) as updated:
                self.assertIsNone(updated.testzip())
                for filename in ['xray','v2ray']:
                    self.assertEqual(hashlib.sha256(baseline.read(filename)).hexdigest(),
                                     hashlib.sha256(updated.read(filename)).hexdigest())
                for filename in ['geoip.dat','geosite.dat']:
                    self.assertEqual(updated.read(filename),(directory/filename).read_bytes())


if __name__ == '__main__':
    unittest.main(verbosity=2)
