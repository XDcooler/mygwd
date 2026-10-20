"""Update failure injection with private files and service command adapters.

No real install, service restart, firewall change or download is performed.
"""
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
HELPERS = REPO/'resource/client/ui-script'
STAGES = ('preUpdate preDownload cleanDep preConf SmartDNSset installXray '
          'installIptablesProxy de_GWDconnect installDep installNginx nginxSet '
          'installWebUI installPihole piholeSet postInstall').split()
broker = types.ModuleType('panel_broker')
exec(compile((HELPERS/'ui-panel').read_text(), 'ui-panel', 'exec'), broker.__dict__)


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root/'updates'
        self.events = self.root/'events'
        self.events.touch()
        source = (REPO/'client').read_text().rsplit('\ncase "${1:-}" in\n', 1)[0]
        locations = {'/etc/':str(self.root/'etc')+'/', '/tmp/':str(self.root/'tmp')+'/',
                     '/var/lib/de-gwd-update':str(self.state), '/opt/de_GWD':str(self.root/'gateway'),
                     '/var/www/html':str(self.root/'web'),
                     '/usr/sbin/php-fpm8.4':str(self.root/'php-fpm8.4')}
        for index, original in enumerate(locations):
            source = source.replace(original, '__FIXTURE_PATH_'+str(index)+'__')
        for index, destination in enumerate(locations.values()):
            source = source.replace('__FIXTURE_PATH_'+str(index)+'__', destination)
        self.source = self.root/'client'
        self.source.write_text(source)
        self.env = dict(os.environ, EVENTS=str(self.events))

    def shell(self, script, **environment):
        return subprocess.run(['bash', '-c', '''
clear() { :; }
dpkg() { echo arm64; }
source "$1"
red() { echo "$*" >&2; }
blue() { echo "$*"; }
green() { :; }
requireDebian13() { return 0; }
flock() { return 0; }
''' + script, 'fixture', str(self.source)], env=dict(self.env, **environment),
            text=True, capture_output=True, timeout=15)

    def update(self, failure='', cron_failure='0'):
        functions = '\n'.join(name+'() { echo '+name+' >>"$EVENTS"; '
            + ('clientCronStopped=1; ' if name == 'preDownload' else '')
            + '[[ $FAIL_STAGE != '+name+' ]]; }' for name in STAGES)
        return self.shell(functions + '''
service() { echo "service $*" >>"$EVENTS"; return "$CRON_FAILURE"; }
updateGWD --worker
''', FAIL_STAGE=failure, CRON_FAILURE=cron_failure)

    def test_each_failed_stage_is_persisted_and_later_stages_do_not_run(self):
        for stage in STAGES:
            with self.subTest(stage=stage):
                self.events.write_text('')
                result = self.update(stage)
                self.assertNotEqual(result.returncode, 0, result.stdout+result.stderr)
                state = json.loads((self.state/'status.json').read_text())
                self.assertEqual((state['status'], state['phase']), ('failed', stage))
                events = self.events.read_text().splitlines()
                self.assertEqual([e for e in events if e in STAGES], STAGES[:STAGES.index(stage)+1])
                self.assertEqual('service cron restart' in events, stage != 'preUpdate')
                self.assertNotIn('[done]', result.stdout)

    def test_success_and_cron_restoration_failure_have_distinct_results(self):
        self.assertEqual(self.update().returncode, 0)
        self.assertEqual(json.loads((self.state/'status.json').read_text())['status'], 'succeeded')
        self.assertNotEqual(self.update('installWebUI', '1').returncode, 0)
        state = json.loads((self.state/'status.json').read_text())
        self.assertEqual((state['status'], state['phase']), ('failed', 'restoreCron'))

    def test_signal_marks_failure_and_restores_cron(self):
        functions = '\n'.join(name+'() { return 0; }' for name in STAGES)
        result = self.shell(functions + '''
preDownload() { clientCronStopped=1; }
cleanDep() { python3 -c 'import os,signal; os.kill(os.getppid(),signal.SIGTERM)'; }
service() { echo "service $*" >>"$EVENTS"; }
updateGWD --worker
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('service cron restart', self.events.read_text())
        self.assertEqual(json.loads((self.state/'status.json').read_text())['status'], 'failed')

    def test_worker_uses_independent_unit_and_private_immutable_copy(self):
        result = self.shell('''
systemctl() { return 3; }
systemd-run() { printf '%s\\n' "$@" >>"$EVENTS"; }
startDetachedUpdate
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.state/'client').read_bytes(), self.source.read_bytes())
        self.assertEqual((self.state/'client').stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.state/'update.log').stat().st_mode & 0o777, 0o600)
        args = self.events.read_text().splitlines()
        self.assertIn('--unit=de-gwd-update', args)
        self.assertEqual(args[-2:], [str(self.state/'client'), '--update'])
        self.assertNotIn('--scope', args)

    def test_running_worker_does_not_replace_script_or_log(self):
        self.state.mkdir()
        (self.state/'client').write_text('running worker')
        (self.state/'update.log').write_text('existing log')
        result = self.shell('systemctl() { return 0; }; startDetachedUpdate')
        self.assertEqual(result.returncode, 0)
        self.assertEqual((self.state/'client').read_text(), 'running worker')
        self.assertEqual((self.state/'update.log').read_text(), 'existing log')

    def test_proxy_waits_for_xray_and_retries_rule_start(self):
        result = self.shell('''
ready=0; rules=0
sleep() { :; }
ss() { echo LISTEN; }
systemctl() {
  echo "$*" >>"$EVENTS"
  if [[ $1 == is-active ]]; then ready=$((ready+1)); [[ $ready -ge 3 ]];
  else rules=$((rules+1)); [[ $rules -ge 2 ]]; fi
}
restartProxyRules
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        events = self.events.read_text().splitlines()
        self.assertEqual(events[:3], ['is-active --quiet vtrui']*3)
        self.assertEqual(events[3:], ['restart iptables-proxy']*2)

    def test_inactive_xray_leaves_proxy_rules_untouched(self):
        result = self.shell('''
sleep() { :; }
systemctl() { echo "$*" >>"$EVENTS"; return 3; }
restartProxyRules
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('restart iptables-proxy', self.events.read_text())

    def test_post_install_starts_xray_and_proxy_before_rule_updates(self):
        gateway = self.root/'gateway'
        gateway.mkdir()
        (gateway/'0conf').write_text('{}')
        (self.root/'tmp').mkdir()
        helpers = ('ui_4h ui_2h ui-piholeConfig ui-NodeOne ui-NodeNFhide ui-NodeCUhide '
                   'ui-NodeDThide ui-offUDP ui_am4 ui-block53off').split()
        for name in helpers:
            helper = gateway/name
            helper.write_text('#!/bin/bash\necho '+name+' >>"$EVENTS"\n[[ $FAIL_HELPER != '+name+' ]]\n')
            helper.chmod(0o755)
        script = '''
nginx() { echo php8.4-fpm.sock; }
systemctl() { echo "systemctl $*" >>"$EVENTS"; }
service() { echo "service $*" >>"$EVENTS"; }
ss() { echo LISTEN; }
crontab() { return 0; }
dig() { echo 93.184.215.14; }
postInstall
'''
        result = self.shell(script, FAIL_HELPER='')
        self.assertEqual(result.returncode, 0, result.stderr)
        events = self.events.read_text().splitlines()
        self.assertLess(events.index('systemctl restart vtrui'), events.index('systemctl restart iptables-proxy'))
        self.assertLess(events.index('systemctl restart iptables-proxy'), events.index('ui_am4'))
        self.events.write_text('')
        result = self.shell(script, FAIL_HELPER='ui-NodeOne')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('ui_am4', self.events.read_text())

    def test_panel_deployment_refreshes_timestamps_before_restarting_fpm(self):
        web = self.root/'web'
        (web/'act').mkdir(parents=True)
        for name in ('auth.php', 'act/test.php'):
            path = web/name
            path.write_text('<?php echo "new authentication code";')
            os.utime(path, (315504000, 315504000))
        fpm = self.root/'php-fpm8.4'
        fpm.write_text('#!/bin/bash\necho "fpm $*" >>"$EVENTS"\n')
        fpm.chmod(0o755)
        result = self.shell('''
systemctl() { echo "$*" >>"$EVENTS"; }
refreshPanelPHP
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ('auth.php', 'act/test.php'):
            self.assertGreater((web/name).stat().st_mtime, 315504000)
        self.assertEqual(self.events.read_text().splitlines(),
                         ['fpm -t', 'restart php8.4-fpm', 'is-active --quiet php8.4-fpm'])

    def test_failed_update_restores_dns_port_resolver_and_bootstrap_v2dns(self):
        gateway = self.root/'gateway'
        (gateway/'v2dns').mkdir(parents=True)
        (gateway/'v2dns/config.json').write_text(json.dumps({'inbounds':[{'tag':'dnsin','port':53}]}))
        native = self.root/'etc/pihole'
        native.mkdir(parents=True)
        (native/'pihole.toml').write_text('[dns]\nport = 53\n')
        resolver_target = self.root/'original-resolv.conf'
        resolver_target.write_text('nameserver 127.0.0.1\n')
        resolver = self.root/'etc/resolv.conf'
        resolver.symlink_to(resolver_target)
        functions = '\n'.join(name+'() { return 0; }' for name in STAGES)
        result = self.shell(functions + '''
preDownload() {
  captureUpdateDNS || return 1
  clientNetworkChanged=1
  clientCronStopped=1
  printf '[dns]\\nport = 1053\\n' >"$NATIVE"
  rm "$RESOLVER"
  printf 'nameserver 223.5.5.5\\n' >"$RESOLVER"
  return 1
}
timeout() { shift; "$@"; }
pihole-FTL() { echo "pihole $*" >>"$EVENTS"; printf '[dns]\\nport = %s\\n' "$3" >"$NATIVE"; }
systemctl() { echo "systemctl $*" >>"$EVENTS"; }
service() { echo "service $*" >>"$EVENTS"; }
dig() { echo 93.184.215.14; }
updateGWD --worker
''', NATIVE=str(native/'pihole.toml'), RESOLVER=str(resolver))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads((self.state/'status.json').read_text())['phase'], 'preDownload')
        self.assertIn('port = 53', (native/'pihole.toml').read_text())
        self.assertTrue(resolver.is_symlink())
        self.assertEqual(resolver.read_text(), 'nameserver 127.0.0.1\n')
        self.assertEqual(json.loads((gateway/'v2dns/config.json').read_text())['inbounds'][0]['port'], 5350)
        events = self.events.read_text().splitlines()
        self.assertLess(events.index('systemctl restart v2dns'), events.index('pihole --config dns.port 53'))
        self.assertIn('service cron restart', events)

    def test_dns_readiness_rejects_servfail_and_checks_both_transports(self):
        result = self.shell('''
sleep() { :; }
dig() { echo "$*" >>"$EVENTS"; echo ';; status: SERVFAIL'; }
waitForClientDNS
''')
        self.assertNotEqual(result.returncode, 0)
        self.events.write_text('')
        result = self.shell('''
dig() { echo "$*" >>"$EVENTS"; echo 93.184.215.14; }
waitForClientDNS
''')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(self.events.read_text().splitlines()), 2)
        self.assertIn('+notcp', self.events.read_text())
        self.assertIn('+tcp', self.events.read_text())

    def test_direct_forwarding_is_persisted_for_systemd_boot(self):
        result = self.shell('''
sysctl() { echo "$*" >>"$EVENTS"; }
configureClientForwarding
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        path = self.root/'etc/sysctl.d/90-de-gwd-forwarding.conf'
        self.assertIn('net.ipv4.ip_forward = 1', path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.events.read_text().strip(), '-q -w net.ipv4.ip_forward=1')


class PanelUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.private = self.root/'private'
        self.config = self.root/'0conf'
        self.config.write_text('{"v2node":[{"name":"fixture"}]}')

    def main(self, params):
        output = io.StringIO()
        with patch.object(broker, 'ROOT', self.root), patch.object(broker, 'CONFIG', self.config), \
             patch.object(broker, 'PRIVATE', self.private), \
             patch.object(broker.os, 'geteuid', return_value=0), \
             patch.object(broker, 'protect_configs'), patch.object(sys, 'argv', ['ui-panel']), \
             patch.object(sys, 'stdin', io.TextIOWrapper(io.BytesIO(json.dumps(
                 {'action':'snapshot','params':params}).encode()))), patch.object(sys, 'stdout', output):
            broker.main()
        return json.loads(output.getvalue())

    def test_empty_php_array_and_object_both_load_dashboard(self):
        for params in ([], {}):
            self.assertTrue(self.main(params)['ok'])
        with self.assertRaises(broker.Rejected):
            self.main(['unexpected'])

    def test_broker_preserves_service_read_access_and_private_master_config(self):
        for name in ('vtrui', 'vtrui1', 'xDNSs', 'RproxyC', 'RproxyS', 'v2dns'):
            (self.root/name).mkdir()
            (self.root/name/'config.json').write_text('{}')
        with patch.object(broker, 'ROOT', self.root), patch.object(broker, 'CONFIG', self.config), \
             patch.object(broker.grp, 'getgrnam', return_value=types.SimpleNamespace(gr_gid=33)), \
             patch.object(broker.os, 'chown') as chown:
            broker.protect_configs()
        ownership = {str(c.args[0]): c.args[1:] for c in chown.call_args_list}
        for name in ('vtrui', 'vtrui1', 'xDNSs', 'RproxyC', 'RproxyS'):
            path = self.root/name/'config.json'
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(ownership[str(path)], (0, 33))
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root/'v2dns/config.json').stat().st_mode & 0o777, 0o600)

    def test_status_detects_interrupted_worker_without_exposing_extra_data(self):
        (self.root/'status.json').write_text(json.dumps(dict(status='running', phase='installWebUI',
            exit_code=0, updated_at=1, secret='must not be returned')))
        with patch.object(broker, 'UPDATES', self.root), patch.object(broker, 'update_running', return_value=False):
            status = broker.update_status()
        self.assertEqual(status['status'], 'interrupted')
        self.assertNotIn('secret', status)

    def test_active_update_blocks_replacing_its_download(self):
        with patch.object(broker, 'update_running', return_value=True), patch.object(broker, 'run') as run:
            with self.assertRaises(broker.Rejected):
                broker.update_download()
            run.assert_not_called()

    def test_console_stop_keeps_worker_script_while_updating(self):
        (self.root/'update').write_text('script')
        with patch.object(broker, 'ROOT', self.root), patch.object(broker, 'run') as run, \
             patch.object(broker, 'update_running', return_value=True):
            broker.perform('installZ', {}, {})
            run.assert_called_once_with(['/usr/bin/systemctl', 'stop', 'updateGWD'])
        self.assertTrue((self.root/'update').exists())

    def test_update_lock_identifies_running_cli_worker(self):
        with (self.root/'update.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(broker, 'UPDATES', self.root):
                self.assertTrue(broker.update_running())


if __name__ == '__main__':
    unittest.main(verbosity=2)
