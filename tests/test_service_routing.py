"""Service routing policy, precedence and transactional failure checks."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
HELPERS = REPO/'resource/client/ui-script'
routing = types.ModuleType('service_routing')
exec(compile((HELPERS/'service-routing.py').read_text(), 'service-routing.py', 'exec'), routing.__dict__)
CATALOG = json.loads((HELPERS/'service-catalog.json').read_text())


class ServiceRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'vtrui').mkdir()
        self.node = {'domain': 'gateway.example:443', 'name': '<script>node</script>',
                     'tls': 'gateway.example', 'uuid': '11111111-1111-4111-8111-111111111111',
                     'path': '/private-test-path'}
        self.config = {'v2node': [self.node], 'address': {'udp': 'on'}}
        self.core = {'outbounds': [{'tag': 'default', 'protocol': 'vless'},
                                  {'tag': 'direct', 'protocol': 'freedom'}],
                     'routing': {'rules': [
                         {'type': 'field', 'ip': ['geoip:private'], 'outboundTag': 'direct'},
                         {'type': 'field', 'inboundTag': ['dnsFlow'], 'outboundTag': 'direct'},
                         {'type': 'field', 'domain': ['domain:custom.example'], 'outboundTag': 'custom'}]}}
        self.site = routing.geodata(REPO/'resource/rules/geosite.dat')
        self.geo = routing.geodata(REPO/'resource/rules/geoip.dat')
        self.key = routing.node_id(self.node)

    def compile(self, policies, core=None):
        return routing.compile_routes(self.config, core or self.core, CATALOG, policies, self.site, self.geo)

    def test_all_services_load_real_categories_and_keep_domain_ip_as_or(self):
        self.assertEqual(len(CATALOG['services']), 26)
        for service in CATALOG['services']:
            for category in service.get('geosite', []):
                self.assertIn(category, self.site)
        policies = {service['id']: {'mode': 'node', 'node': self.key} for service in CATALOG['services']}
        core, _, _, networks, domains = self.compile(policies)
        self.assertGreater(len(networks), 0)
        self.assertIn('chatgpt.com', domains)
        self.assertTrue(all(not ('domain' in rule and 'ip' in rule) for rule in core['routing']['rules']))
        for outbound in core['outbounds']:
            if outbound['tag'].startswith('svc-node-'):
                self.assertIs(outbound['streamSettings']['tlsSettings']['allowInsecure'], False)
                self.assertEqual(outbound['streamSettings']['sockopt']['mark'], 255)

    def test_console_source_precedes_service_but_private_and_dns_stay_direct(self):
        policies = {'xbox': {'mode': 'node', 'node': self.key, 'devices': ['10.0.0.50']},
                    'openai': {'mode': 'direct'}}
        core, _, devices, _, _ = self.compile(policies)
        rules = core['routing']['rules']
        self.assertEqual(rules[:2], self.core['routing']['rules'][:2])
        self.assertEqual(rules[2]['source'], ['10.0.0.50'])
        self.assertEqual(devices, {'10.0.0.50'})
        self.assertEqual(rules[-1], self.core['routing']['rules'][-1])

    def test_reapply_is_idempotent_and_old_netflix_rule_is_migrated(self):
        self.core['outbounds'].append({'tag': 'netflix', 'protocol': 'freedom'})
        self.core['routing']['rules'].append({'type': 'field', 'domain': ['geosite:netflix'], 'outboundTag': 'netflix'})
        policy = {'netflix': {'mode': 'node', 'node': self.key}}
        first = self.compile(policy)[0]
        second = self.compile(policy, first)[0]
        self.assertEqual(first, second)
        self.assertNotIn('netflix', [out['tag'] for out in first['outbounds']])

    def test_missing_geosite_uses_explicit_domains_without_invalid_category(self):
        core = routing.compile_routes(self.config, self.core, CATALOG,
            {'openai': {'mode': 'node', 'node': self.key}}, {}, {})[0]
        rule = next(rule for rule in core['routing']['rules'] if rule.get('outboundTag', '').startswith('svc-node'))
        self.assertIn('domain:chatgpt.com', rule['domain'])
        self.assertFalse(any(value.startswith('geosite:') for value in rule['domain']))

    def test_invalid_missing_node_and_console_addresses_are_rejected(self):
        cases = [{'unknown': {'mode': 'direct'}}, {'openai': {'mode': 'node', 'node': 'missing'}},
                 {'xbox': {'mode': 'direct', 'devices': ['1.1.1.1']}},
                 {'openai': {'mode': 'direct', 'devices': ['10.0.0.20']}},
                 {'xbox': {'mode': 'node', 'node': self.key, 'devices': [123]}},
                 {'xbox': {'mode': 'default', 'devices': ['10.0.0.20']},
                  'nintendo': {'mode': 'default', 'devices': ['10.0.0.20']}}]
        for policies in cases:
            with self.subTest(policies=policies), self.assertRaises(ValueError):
                self.compile(policies)

    def test_status_never_contains_node_uuid_transport_path_or_password(self):
        self.config['address']['PWD'] = 'a'*64
        result = json.dumps(routing.status(self.root, CATALOG, self.config))
        for secret in (self.node['uuid'], self.node['path'], 'a'*64):
            self.assertNotIn(secret, result)
        self.assertIn(self.key, result)

    def test_legacy_netflix_selection_is_preserved_and_missing_node_is_visible(self):
        self.config['v2nodeDIV'] = {'nodeNF': dict(self.node, display='on')}
        self.assertEqual(routing.saved_policies(self.config)['netflix']['node'], self.key)
        self.config['v2node'] = []
        self.assertEqual(routing.saved_policies(self.config)['netflix']['node'], 'legacy-node-missing')

    def prepare_apply(self):
        (self.root/'0conf').write_text(json.dumps(self.config))
        (self.root/'vtrui/config.json').write_text(json.dumps(self.core))
        for filename in ('geosite.dat', 'geoip.dat'):
            (self.root/'vtrui'/filename).write_bytes((REPO/'resource/rules'/filename).read_bytes())
        return {path: path.read_bytes() for path in (self.root/'0conf', self.root/'vtrui/config.json')}

    def test_invalid_core_never_replaces_working_files(self):
        originals = self.prepare_apply()
        with patch.object(routing.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)), self.assertRaises(ValueError):
            routing.apply(self.root, CATALOG, self.config, {'openai': {'mode': 'direct'}}, restart=False)
        self.assertEqual({path: path.read_bytes() for path in originals}, originals)
        self.assertFalse((self.root/'IPconsoleSET').exists())

    def test_failed_service_restart_restores_all_files_and_preserves_private_config(self):
        originals = self.prepare_apply()
        def run(argv, **kwargs):
            if argv[:2] == ['systemctl', 'restart'] and argv[-1] == 'iptables-proxy' and kwargs.get('check'):
                raise subprocess.CalledProcessError(1, argv)
            return subprocess.CompletedProcess(argv, 0)
        with patch.object(routing.subprocess, 'run', side_effect=run), self.assertRaises(ValueError):
            routing.apply(self.root, CATALOG, self.config,
                {'xbox': {'mode': 'node', 'node': self.key, 'devices': ['10.0.0.50']}})
        self.assertEqual({path: path.read_bytes() for path in originals}, originals)
        self.assertFalse((self.root/'services-dnsmasq.conf').exists())


if __name__ == '__main__':
    unittest.main()
