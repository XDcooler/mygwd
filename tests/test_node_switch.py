"""Node switching and rollback fixtures; never restart a host service."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
HELPERS = REPO/'resource/client/ui-script'
routing = types.ModuleType('service_routing')
exec(compile((HELPERS/'service-routing.py').read_text(), 'service-routing.py', 'exec'), routing.__dict__)
broker = types.ModuleType('panel_broker')
exec(compile((HELPERS/'ui-panel').read_text(), 'ui-panel', 'exec'), broker.__dict__)
CATALOG = {'services': [{'id': 'appletv', 'domains': ['tv.apple.com']}]}


class NodeSwitchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'vtrui').mkdir()
        self.first = {'domain': 'old.example:443', 'name': 'Old', 'tls': 'old.example',
                      'uuid': '11111111-1111-4111-8111-111111111111', 'path': '/old-path'}
        self.second = {'domain': 'new.example', 'name': 'New', 'tls': 'new.example',
                       'uuid': '22222222-2222-4222-8222-222222222222', 'path': '/new-path'}
        self.config = {'address': {'udp': 'on', 'localIP': '10.0.0.1', 'upstreamIP': '10.0.0.2', 'PWD': 'a'*64},
                       'v2node': [self.first, self.second],
                       'update': {'v2node': {'domain': 'old.example', 'port': '443'}},
                       'serviceRouting': {'policies': {'appletv': {'mode': 'node', 'node': 'node-stale'}}}}
        self.core = {'inbounds': [], 'outbounds': [routing.make_outbound(self.first, 'default'),
                     {'tag': 'direct', 'protocol': 'freedom'},
                     routing.make_outbound(self.first, 'svc-default'),
                     routing.make_outbound(self.first, 'svc-node-stale')],
                     'routing': {'rules': [{'type': 'field', 'domain': ['domain:tv.apple.com'],
                                            'outboundTag': 'svc-node-stale'}]}}
        self.files = [self.root/'0conf', self.root/'vtrui/config.json']
        for path, value, mode in zip(self.files, [self.config, self.core], [0o600, 0o640]):
            path.write_text(json.dumps(value)); path.chmod(mode)
        self.before = {path: path.read_bytes() for path in self.files}
        self.commands = []

    def run_command(self, argv, **kwargs):
        self.commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=b'LISTEN', stderr=b'')

    def switch(self, runner=None, ready=True):
        with patch.object(routing.subprocess, 'run', side_effect=runner or self.run_command), \
             patch.object(routing, 'proxy_ready', return_value=ready):
            return routing.switch_node(self.root, self.config, 1)

    def assert_originals(self):
        self.assertEqual({path: path.read_bytes() for path in self.files}, self.before)
        self.assertEqual([path.stat().st_mode & 0o777 for path in self.files], [0o600, 0o640])

    def test_switch_with_stale_policy_updates_default_and_followers_but_keeps_pins(self):
        result = self.switch()
        self.assertEqual(result, {'saved': True, 'nodeIndex': 1, 'recoveredPolicies': 1})
        core = json.loads(self.files[1].read_text())
        for tag in ('default', 'svc-default'):
            out = next(out for out in core['outbounds'] if out['tag'] == tag)
            self.assertEqual(out['settings']['vnext'][0]['address'], 'new.example')
        expected_pin = dict(self.core['outbounds'][-1], tag='svc-' + routing.node_id(self.first))
        self.assertEqual(core['outbounds'][-1], expected_pin)
        expected_rule = dict(self.core['routing']['rules'][0], outboundTag=expected_pin['tag'])
        self.assertEqual(core['routing']['rules'], [expected_rule])
        saved = json.loads(self.files[0].read_text())
        self.assertEqual(saved['update']['v2node']['domain'], 'new.example')
        self.assertEqual(saved['serviceRouting']['policies']['appletv']['node'], routing.node_id(self.first))
        self.assertEqual(routing.selected_node(saved, core), 1)
        self.assertEqual([p.stat().st_mode & 0o777 for p in self.files], [0o600, 0o640])

    def test_rejected_candidate_does_not_write_or_restart(self):
        def reject(argv, **kwargs):
            self.commands.append(argv)
            return subprocess.CompletedProcess(argv, 1)
        with self.assertRaises(ValueError): self.switch(reject)
        self.assert_originals()
        self.assertFalse(any(cmd[0] == 'systemctl' for cmd in self.commands))
        self.assertFalse(list((self.root/'vtrui').glob('.services-*')))

    def test_unrecoverable_pin_does_not_block_default_switch_or_change_its_exit(self):
        self.config['v2node'][0]['uuid'] = '33333333-3333-4333-8333-333333333333'
        self.files[0].write_text(json.dumps(self.config))
        result = self.switch()
        self.assertEqual(result['recoveredPolicies'], 0)
        saved = json.loads(self.files[0].read_text())
        core = json.loads(self.files[1].read_text())
        self.assertEqual(saved['serviceRouting'], self.config['serviceRouting'])
        self.assertEqual(core['outbounds'][-1], self.core['outbounds'][-1])
        self.assertEqual(core['routing'], self.core['routing'])

    def test_interruption_after_files_changed_restores_originals(self):
        calls = 0
        def interrupt(argv, **kwargs):
            nonlocal calls
            if argv[:2] == ['systemctl', 'restart']:
                calls += 1
                if calls == 1: raise InterruptedError('fixture signal')
            return self.run_command(argv, **kwargs)
        with self.assertRaises(routing.SwitchFailure): self.switch(interrupt)
        self.assert_originals()

    def test_failed_restart_restores_both_files_and_restarts_previous_proxy(self):
        restarts = []
        def fail_once(argv, **kwargs):
            if argv[:2] == ['systemctl', 'restart']:
                restarts.append(json.loads(self.files[1].read_text())['outbounds'][0]['settings']['vnext'][0]['address'])
                if len(restarts) == 1: raise subprocess.CalledProcessError(1, argv)
            return self.run_command(argv, **kwargs)
        with self.assertRaises(routing.SwitchFailure) as error: self.switch(fail_once)
        self.assertTrue(error.exception.recovered)
        self.assert_originals()
        self.assertEqual(restarts, ['new.example', 'old.example'])

    def test_missing_listener_rolls_back_even_when_restart_returned_success(self):
        with patch.object(routing.subprocess, 'run', side_effect=self.run_command), \
             patch.object(routing, 'proxy_ready', side_effect=[False, True]), \
             self.assertRaises(routing.SwitchFailure) as error:
            routing.switch_node(self.root, self.config, 1)
        self.assertTrue(error.exception.recovered)
        self.assert_originals()

    def test_recovery_failure_is_reported(self):
        def fail(argv, **kwargs):
            if argv[:2] == ['systemctl', 'restart']: raise subprocess.CalledProcessError(1, argv)
            return self.run_command(argv, **kwargs)
        with self.assertRaises(routing.SwitchFailure) as error: self.switch(fail)
        self.assertFalse(error.exception.recovered)
        self.assert_originals()

    def test_partial_write_failure_restores_original_bytes(self):
        real = routing.atomic_write
        calls = 0
        def fail(path, data, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2: raise OSError('fixture disk failure')
            return real(path, data, **kwargs)
        with patch.object(routing, 'atomic_write', side_effect=fail), self.assertRaises(routing.SwitchFailure):
            self.switch()
        self.assert_originals()

    def test_inactive_proxy_is_stopped_on_rollback(self):
        def fail(argv, **kwargs):
            if argv[:2] == ['systemctl', 'is-active']: return subprocess.CompletedProcess(argv, 3)
            if argv[:2] == ['systemctl', 'restart']: raise subprocess.CalledProcessError(1, argv)
            return self.run_command(argv, **kwargs)
        with self.assertRaises(routing.SwitchFailure) as error: self.switch(fail)
        self.assertTrue(error.exception.recovered)
        self.assertIn(['systemctl', 'stop', 'vtrui'], self.commands)
        self.assert_originals()

    def test_bad_index_or_node_cannot_change_configuration(self):
        with patch.object(routing.subprocess, 'run') as run:
            for index in (-1, 2, None):
                with self.assertRaises(ValueError): routing.switch_node(self.root, self.config, index)
            self.config['v2node'][1]['uuid'] = 'invalid'
            with self.assertRaises(ValueError): routing.switch_node(self.root, self.config, 1)
            run.assert_not_called()
        self.assert_originals()

    def test_selected_node_normalizes_default_port_and_checks_uuid_path_and_tls(self):
        self.assertEqual(routing.selected_node(self.config, self.core), 0)
        self.config['v2node'][0]['domain'] = 'old.example'
        self.assertEqual(routing.selected_node(self.config, self.core), 0)
        for key, value in [('uuid', self.second['uuid']), ('path', '/different'), ('tls', 'different.example')]:
            modified = copy.deepcopy(self.config); modified['v2node'][0][key] = value
            self.assertIsNone(routing.selected_node(modified, self.core))

    def test_proxy_readiness_requires_tcp_and_enabled_udp(self):
        commands = []
        def run(argv, **kwargs):
            commands.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout=b'LISTEN')
        with patch.object(routing.subprocess, 'run', side_effect=run):
            self.assertTrue(routing.proxy_ready(self.config))
        self.assertTrue(any('-lnt' in cmd for cmd in commands))
        self.assertTrue(any('-lnu' in cmd for cmd in commands))
        commands.clear()
        with patch.object(routing.subprocess, 'run', side_effect=run):
            self.assertTrue(routing.proxy_ready({'address': {'udp': 'off'}}))
        self.assertFalse(any('-lnu' in cmd for cmd in commands))

    def test_stale_service_id_is_recovered_only_from_matching_compiled_outbound(self):
        policy = routing.recover_node_references(self.config, self.core, self.config['serviceRouting']['policies'])
        self.assertEqual(policy['appletv']['node'], routing.node_id(self.first))
        compiled = routing.compile_routes(self.config, self.core, CATALOG,
                    self.config['serviceRouting']['policies'], {}, {})
        self.assertEqual(compiled[1], policy)
        self.assertEqual(self.config['serviceRouting']['policies']['appletv']['node'], 'node-stale')

    def test_stale_service_id_is_not_rebound_by_hostname_alone(self):
        for key, value in [('uuid', self.second['uuid']), ('tls', 'different.example'), ('path', '/different'),
                           ('domain', 'old.example:8443')]:
            changed = copy.deepcopy(self.config); changed['v2node'][0][key] = value
            policy = routing.recover_node_references(changed, self.core, changed['serviceRouting']['policies'])
            self.assertEqual(policy['appletv']['node'], 'node-stale')
            with self.assertRaises(ValueError): routing.normalize(policy, CATALOG, changed)

    def test_replacing_default_port_spelling_preserves_service_binding(self):
        replacement = copy.deepcopy(self.config['v2node']); replacement[0]['domain'] = 'old.example'
        policies = routing.replacement_policies(self.config, self.core, CATALOG, replacement)
        self.assertEqual(policies['appletv']['node'], routing.node_id(replacement[0]))
        with self.assertRaises(ValueError):
            routing.replacement_policies(self.config, self.core, CATALOG, [self.second])

    def test_panel_rejects_removing_pinned_node_before_saving_or_running_helpers(self):
        shutil.copy2(HELPERS/'service-routing.py', self.root/'service-routing.py')
        (self.root/'service-catalog.json').write_text(json.dumps(CATALOG))
        with patch.object(broker, 'ROOT', self.root), patch.object(broker, 'CONFIG', self.files[0]), \
             patch.object(broker, 'helper') as helper, self.assertRaises(broker.Rejected):
            broker.perform('saveNode', {'nodeList': [self.second]}, copy.deepcopy(self.config))
        helper.assert_not_called()
        self.assert_originals()

    def test_panel_saving_equivalent_node_repairs_stale_policy(self):
        shutil.copy2(HELPERS/'service-routing.py', self.root/'service-routing.py')
        (self.root/'service-catalog.json').write_text(json.dumps(CATALOG))
        replacement = copy.deepcopy(self.config['v2node']); replacement[0]['domain'] = 'old.example'
        with patch.object(broker, 'ROOT', self.root), patch.object(broker, 'CONFIG', self.files[0]), \
             patch.object(broker, 'helper', return_value=''), patch.object(broker, 'run', return_value=''):
            broker.perform('saveNode', {'nodeList': replacement}, copy.deepcopy(self.config))
        saved = json.loads(self.files[0].read_text())
        self.assertEqual(saved['serviceRouting']['policies']['appletv']['node'], routing.node_id(replacement[0]))
        self.assertEqual(self.files[1].read_bytes(), self.before[self.files[1]])

    def test_selected_node_wrapper_uses_real_index_not_domain_substring(self):
        shutil.copy2(HELPERS/'service-routing.py', self.root/'service-routing.py')
        result = subprocess.run(['bash', str(HELPERS/'ui-checkNode')],
                                env=dict(os.environ, DE_GWD_ROOT=str(self.root)), capture_output=True, text=True)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, '0'), result.stderr)


if __name__ == '__main__':
    unittest.main()
