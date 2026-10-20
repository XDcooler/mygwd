#!/usr/bin/env python3
"""Run the real proxy helpers against a stateful kernel-command adapter.

These tests verify rule ownership, routing decisions and rollback. They do not
claim that Linux TPROXY, systemd or packet forwarding ran on the host.
"""
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
HELPERS = REPO / 'resource/client/ui-script'
ADAPTER = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
p = Path(os.environ['PROXY_TEST_STATE'])
s = json.loads(p.read_text())
s['commands'].append([name] + args)
status = 0
output = ''
fail = os.environ.get('PROXY_TEST_FAIL', '')
if fail and fail in ' '.join([name] + args) and not s.get('failed'):
    s['failed'] = True
    status = 1
elif name == 'id':
    output = '0'
elif name == 'systemctl':
    if args[:2] == ['is-active', '--quiet'] and os.environ.get('PROXY_TEST_INACTIVE'):
        status = 3
    elif args[:1] == ['restart'] and os.environ.get('PROXY_TEST_RESTART_FAIL'):
        status = 1
elif name == 'ss':
    if not os.environ.get('PROXY_TEST_NO_LISTENER'):
        output = 'LISTEN 0 4096 127.0.0.1:9896 0.0.0.0:*'
elif name == 'sleep':
    pass
elif name in ('sed', 'sysctl'):
    # A DNS access toggle must never edit host-wide forwarding or sysctls.
    status = 99
elif name == 'ip':
    if args == ['-o', 'route', 'show', 'default']:
        output = 'default via 192.168.1.1 dev eth0'
    elif args[0] == 'rule':
        entry = args[2:]
        if args[1] == 'add':
            if entry in s['rules']:
                status = 2
            else:
                s['rules'].append(entry)
        elif args[1] == 'del':
            if entry in s['rules']:
                s['rules'].remove(entry)
            else:
                status = 2
    elif args[0] == 'route' and args[1] in ('replace', 'del'):
        entry = args[2:]
        if args[1] == 'replace':
            if entry not in s['routes']:
                s['routes'].append(entry)
        elif entry in s['routes']:
            s['routes'].remove(entry)
        else:
            status = 2
elif name == 'iptables':
    table = 'filter'
    if args[:1] == ['-t']:
        table, args = args[1], args[2:]
    if args == ['-S']:
        p.write_text(json.dumps(s))
        sys.exit(0)
    operation, chain = args[:2]
    key = table + ':' + chain
    body = args[2:]
    if operation == '-N':
        if key in s['chains']:
            status = 1
        else:
            s['chains'][key] = []
    elif operation == '-S':
        status = 0 if key in s['chains'] else 1
    elif key not in s['chains']:
        status = 1
    elif operation == '-A':
        s['chains'][key].append(body)
    elif operation == '-C':
        status = 0 if body in s['chains'][key] else 1
        if os.environ.get('PROXY_TEST_RULE_CHECK_OUTPUT'):
            output = 'TPROXY rule diagnostic'
    elif operation == '-D':
        if body in s['chains'][key]:
            s['chains'][key].remove(body)
        else:
            status = 1
    elif operation == '-F':
        s['chains'][key] = []
    elif operation == '-X':
        # A real chain cannot be removed while another chain references it.
        referenced = any('-j' in rule and rule[rule.index('-j')+1] == chain
                         for rules in s['chains'].values() for rule in rules)
        if s['chains'][key] or referenced:
            status = 1
        else:
            del s['chains'][key]
elif name == 'ipset':
    operation = args[0]
    if operation == 'create':
        if args[1] not in s['sets']:
            s['sets'][args[1]] = []
        elif '-exist' not in args:
            status = 1
    elif operation == 'flush':
        s['sets'][args[1]] = []
    elif operation == 'destroy':
        if args[1] in s['sets']:
            del s['sets'][args[1]]
        else:
            status = 1
    elif operation == 'add':
        s['sets'][args[1]].append(args[2])
    elif operation == 'restore':
        for line in sys.stdin.read().splitlines():
            if not line.strip():
                continue
            command, target, address = line.split()
            if command != 'add' or target not in s['sets']:
                status = 1
                break
            s['sets'][target].append(address)
else:
    raise SystemExit('Unexpected adapter command: ' + name)
p.write_text(json.dumps(s))
if output:
    print(output)
sys.exit(status)
'''


class TransparentProxyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='de-gwd-proxy-test-')
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.root = self.directory/'de_GWD'
        self.root.mkdir()
        self.bin = self.directory/'bin'
        self.bin.mkdir()
        for name in ['ui-proxyRules', 'ui-setUDP', 'ui-onUDP', 'ui-offUDP', 'ui-checkUDP',
                     'ui-block53on', 'ui-block53off']:
            shutil.copy2(HELPERS/name, self.root/name)
        for name in ['id', 'ip', 'iptables', 'ipset', 'systemctl', 'ss', 'sleep', 'sed', 'sysctl']:
            (self.bin/name).write_text(ADAPTER)
            (self.bin/name).chmod(0o755)
        if shutil.which('jq') is None:
            runner = Path('/workspace/.cloud-setup/de_GWD/run')
            if not runner.exists():
                raise RuntimeError('Install jq or the documented Debian 11 test rootfs.')
            (self.bin/'jq').write_text('#!/bin/bash\nexec /workspace/.cloud-setup/de_GWD/run /usr/bin/jq "$@"\n')
            (self.bin/'jq').chmod(0o755)
        self.config = {'address': {'udp': 'off'}, 'FORWARD': {'block53': 'off'},
                       'preserved': {'example': ['a', 'b']}}
        (self.root/'0conf').write_text(json.dumps(self.config))
        (self.root/'iptables-proxy-up').write_text('#!/bin/bash\noriginal-wrapper\n')
        (self.root/'chnrouteSET').write_text('add chnroute 1.0.1.0/24\n')
        for filename in ['IPxDNSSET', 'IPv2nodeSET', 'IPlistBSET', 'IPlistWSET',
                         'IPlistBlanSET', 'IPlistWlanSET']:
            (self.root/filename).write_text('')
        self.state_path = self.directory/'state.json'
        self.initial = {'commands': [], 'chains': {
            'filter:INPUT': [['-p', 'tcp', '--dport', '22', '-j', 'ACCEPT']],
            'mangle:PREROUTING': [['-m', 'mark', '--mark', '0x42', '-j', 'ACCEPT']],
            'mangle:OUTPUT': [['-m', 'mark', '--mark', '0x42', '-j', 'ACCEPT']],
            'filter:FORWARD': [['-i', 'wg0', '-j', 'ACCEPT']],
            'nat:POSTROUTING': [['-s', '172.16.66.0/24', '-j', 'MASQUERADE']]},
            'sets': {'unrelated': ['198.51.100.2']},
            'rules': [['from', '198.51.100.0/24', 'table', '220']],
            'routes': [['198.51.100.0/24', 'dev', 'eth0', 'table', '220']]}
        self.state_path.write_text(json.dumps(self.initial))
        self.environment = dict(os.environ, PATH=str(self.bin)+':'+os.environ['PATH'],
                                DE_GWD_ROOT=str(self.root),
                                DE_GWD_RUN_DIRECTORY=str(self.directory/'run'),
                                PROXY_TEST_STATE=str(self.state_path))

    def run_helper(self, name, *arguments, **environment):
        return subprocess.run(['bash', str(self.root/name), *arguments],
                              env=dict(self.environment, **environment), capture_output=True, text=True)

    def state(self):
        return json.loads(self.state_path.read_text())

    def assert_external_rules_preserved(self):
        state = self.state()
        for chain, rules in self.initial['chains'].items():
            self.assertEqual([rule for rule in state['chains'][chain]
                              if not any(value.startswith('DE_GWD_') for value in rule)], rules)
        self.assertEqual(state['sets']['unrelated'], self.initial['sets']['unrelated'])
        self.assertIn(self.initial['rules'][0], state['rules'])
        self.assertIn(self.initial['routes'][0], state['routes'])

    def decision(self, protocol='tcp', source='192.168.1.2', destination='203.0.113.9', mark=0):
        state = self.state()
        packet = {'tcp': protocol == 'tcp', 'udp': protocol == 'udp',
                  'src': ipaddress.ip_address(source), 'dst': ipaddress.ip_address(destination), 'mark': mark}
        def evaluate(chain):
            for rule in state['chains']['mangle:'+chain]:
                if '-p' in rule and not packet[rule[rule.index('-p')+1]]:
                    continue
                if '--mark' in rule:
                    offset = rule.index('--mark')
                    match = packet['mark'] == int(rule[offset+1], 0)
                    if '!' in rule[:offset]:
                        match = not match
                    if not match:
                        continue
                if '--match-set' in rule:
                    offset = rule.index('--match-set')
                    target, direction = rule[offset+1:offset+3]
                    if not any(packet[direction] in ipaddress.ip_network(address, strict=False)
                               for address in state['sets'][target]):
                        continue
                target = rule[rule.index('-j')+1]
                if target == 'MARK':
                    packet['mark'] = int(rule[rule.index('--set-mark')+1], 0)
                elif target == 'RETURN':
                    return
                elif target == 'TPROXY':
                    return 'TPROXY'
                elif evaluate(target) == 'TPROXY':
                    return 'TPROXY'
        return evaluate('DE_GWD_PREROUTING'), packet['mark']

    def test_restart_and_stop_preserve_external_firewall_and_routes(self):
        for arguments in [('up', 'tcp'), ('up', 'tcpudp'), ('down',), ('down',)]:
            result = self.run_helper('ui-proxyRules', *arguments)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assert_external_rules_preserved()
        self.assertEqual(self.state()['chains'], self.initial['chains'])
        self.assertEqual(self.state()['rules'], self.initial['rules'])
        self.assertEqual(self.state()['routes'], self.initial['routes'])

    def test_packets_preserve_direct_bypass_and_udp_mode(self):
        self.assertEqual(self.run_helper('ui-proxyRules', 'up', 'tcp').returncode, 0)
        self.assertEqual(self.decision(), ('TPROXY', 9))
        self.assertEqual(self.decision(destination='192.168.1.9'), (None, 0))
        self.assertEqual(self.decision(destination='1.0.1.9'), (None, 0))
        self.assertEqual(self.decision(mark=255), (None, 255))
        self.assertEqual(self.decision(mark=9), ('TPROXY', 9))
        self.assertEqual(self.decision(protocol='udp'), (None, 0))
        self.assertEqual(self.run_helper('ui-proxyRules', 'up', 'tcpudp').returncode, 0)
        self.assertEqual(self.decision(protocol='udp'), ('TPROXY', 9))
        self.assertEqual(self.decision(protocol='udp', mark=255), (None, 255))

    def test_blacklist_priority_and_node_escape_are_preserved(self):
        (self.root/'IPlistBSET').write_text('add listB 1.0.1.9\n')
        (self.root/'IPlistBlanSET').write_text('add listBlan 192.168.1.3\n')
        (self.root/'IPlistWlanSET').write_text('add listWlan 192.168.1.4\n')
        (self.root/'IPv2nodeSET').write_text('add v2node 203.0.113.9\n')
        result = self.run_helper('ui-proxyRules', 'up', 'tcpudp')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.decision(destination='1.0.1.9'), ('TPROXY', 9))
        self.assertEqual(self.decision(source='192.168.1.3', destination='192.168.1.9'), ('TPROXY', 9))
        self.assertEqual(self.decision(source='192.168.1.4', destination='203.0.113.8'), (None, 0))
        self.assertEqual(self.decision(source='192.168.1.3'), (None, 0))
        self.assertEqual(self.decision(source='192.168.1.3', destination='1.0.1.9', mark=255), (None, 255))

    def test_unhealthy_core_does_not_touch_live_rules(self):
        for variable in ['PROXY_TEST_INACTIVE', 'PROXY_TEST_NO_LISTENER']:
            result = self.run_helper('ui-proxyRules', 'up', 'tcpudp', **{variable: '1'})
            self.assertNotEqual(result.returncode, 0)
            state = self.state()
            self.assertEqual(state['chains'], self.initial['chains'])
            self.assertEqual(state['sets'], self.initial['sets'])
            self.assertEqual(state['rules'], self.initial['rules'])

    def test_kernel_failure_rolls_back_owned_changes_and_reports_failure(self):
        result = self.run_helper('ui-proxyRules', 'up', 'tcpudp',
                                 PROXY_TEST_FAIL='-A DE_GWD_PREROUTING -p tcp -m mark --mark 0x9 -j TPROXY')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('partial de_GWD rules were removed', result.stderr)
        self.assert_external_rules_preserved()
        self.assertEqual(self.state()['chains'], self.initial['chains'])
        self.assertEqual(self.state()['sets'], self.initial['sets'])
        self.assertEqual(self.state()['rules'], self.initial['rules'])

    def test_invalid_source_data_preserves_working_rules_and_udp_config(self):
        self.assertEqual(self.run_helper('ui-proxyRules', 'up', 'tcp').returncode, 0)
        before = self.state()
        (self.root/'chnrouteSET').write_text('add chnroute invalid-route\n')
        for helper, arguments in [('ui-proxyRules', ['up', 'tcpudp']), ('ui-onUDP', ['r'])]:
            result = self.run_helper(helper, *arguments)
            self.assertNotEqual(result.returncode, 0)
            for field in ['chains', 'sets', 'rules', 'routes']:
                self.assertEqual(self.state()[field], before[field])
            self.assertEqual(json.loads((self.root/'0conf').read_text()), self.config)

    def test_missing_network_permission_fails_before_mutation(self):
        result = self.run_helper('ui-proxyRules', 'up', 'tcp',
                                 PROXY_TEST_FAIL='iptables -t mangle -S')
        self.assertNotEqual(result.returncode, 0)
        for field in ['chains', 'sets', 'rules', 'routes']:
            self.assertEqual(self.state()[field], self.initial[field])

    def test_source_changes_remove_stale_set_entries(self):
        (self.root/'IPlistBSET').write_text('add listB 203.0.113.1\n')
        self.assertEqual(self.run_helper('ui-proxyRules', 'up', 'tcp').returncode, 0)
        (self.root/'IPlistBSET').write_text('add listB 203.0.113.2\n')
        self.assertEqual(self.run_helper('ui-proxyRules', 'up', 'tcp').returncode, 0)
        self.assertEqual(self.state()['sets']['listB'], ['203.0.113.2'])

    def test_udp_setting_is_saved_and_failed_restart_restores_config(self):
        result = self.run_helper('ui-onUDP')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.root/'0conf').read_text())['address']['udp'], 'on')
        self.assertIn('up tcpudp', (self.root/'iptables-proxy-up').read_text())
        previous_config = (self.root/'0conf').read_bytes()
        previous_wrapper = (self.root/'iptables-proxy-up').read_bytes()
        result = self.run_helper('ui-offUDP', 'r', PROXY_TEST_RESTART_FAIL='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root/'0conf').read_bytes(), previous_config)
        self.assertEqual((self.root/'iptables-proxy-up').read_bytes(), previous_wrapper)
        result = self.run_helper('ui-offUDP', 'r')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.root/'0conf').read_text())['preserved'], self.config['preserved'])
        self.assertIn('up tcp\n', (self.root/'iptables-proxy-up').read_text())

    def test_dns_port_blocking_uses_only_owned_input_chain(self):
        self.config['FORWARD']['block53'] = 'on'
        (self.root/'0conf').write_text(json.dumps(self.config))
        result = self.run_helper('ui-proxyRules', 'up', 'tcp')
        self.assertEqual(result.returncode, 0, result.stderr)
        rules = self.state()['chains']['filter:DE_GWD_INPUT']
        self.assertEqual(len([rule for rule in rules if '--dport' in rule and rule[rule.index('--dport')+1]=='53']), 2)
        self.assert_external_rules_preserved()

    def test_dns_access_toggles_never_change_global_forwarding(self):
        for name, expected in [('ui-block53on', 'on'), ('ui-block53off', 'off')]:
            result = self.run_helper(name)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((self.root/'0conf').read_text())['FORWARD']['block53'], expected)
        commands = self.state()['commands']
        self.assertFalse(any(command[0] in ('sysctl', 'sed') for command in commands))
        self.assertFalse(any(command[:3] == ['systemctl', 'restart', 'systemd-sysctl'] for command in commands))
        self.assert_external_rules_preserved()

    def test_udp_indicator_requires_configured_and_applied_udp_rules(self):
        self.assertEqual(self.run_helper('ui-onUDP').returncode, 0)
        self.assertEqual(self.run_helper('ui-checkUDP').stdout.strip(), 'outline-secondary')
        self.assertEqual(self.run_helper('ui-proxyRules', 'up', 'tcpudp').returncode, 0)
        self.assertEqual(self.run_helper('ui-checkUDP').stdout.strip(), 'success')
        self.assertEqual(self.run_helper('ui-checkUDP', PROXY_TEST_RULE_CHECK_OUTPUT='1').stdout.strip(), 'success')
        self.assertEqual(self.run_helper('ui-checkUDP', PROXY_TEST_INACTIVE='1').stdout.strip(), 'outline-secondary')
        self.assertEqual(self.run_helper('ui-offUDP').returncode, 0)
        self.assertEqual(self.run_helper('ui-checkUDP').stdout.strip(), 'outline-secondary')
        self.assertEqual(self.run_helper('ui-onUDP').returncode, 0)
        self.assertEqual(self.run_helper('ui-proxyRules', 'up', 'tcp').returncode, 0)
        self.assertEqual(self.run_helper('ui-checkUDP').stdout.strip(), 'outline-secondary')


if __name__ == '__main__':
    unittest.main(verbosity=2)
