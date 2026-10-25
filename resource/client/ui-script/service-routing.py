#!/usr/bin/env python3
"""Compile and atomically apply service routes without exposing node credentials."""
import argparse
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid


def varint(data, offset):
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError('Truncated geodata')
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise ValueError('Invalid geodata integer')


def fields(data):
    offset = 0
    while offset < len(data):
        tag, offset = varint(data, offset)
        number, wire = tag >> 3, tag & 7
        if not number:
            raise ValueError('Invalid geodata field')
        if wire == 0:
            value, offset = varint(data, offset)
        elif wire in (1, 2, 5):
            if wire == 2:
                size, offset = varint(data, offset)
            else:
                size = 8 if wire == 1 else 4
            value = data[offset:offset + size]
            offset += size
            if len(value) != size:
                raise ValueError('Truncated geodata field')
        else:
            raise ValueError('Unsupported geodata field')
        yield number, wire, value


def geodata(path):
    result = {}
    if not path.exists():
        return result
    for number, wire, entry in fields(path.read_bytes()):
        if number == 1 and wire == 2:
            values = list(fields(entry))
            tags = [value.decode('ascii').lower() for field, kind, value in values if field == 1 and kind == 2]
            if len(tags) != 1:
                raise ValueError('Invalid geodata category')
            result[tags[0]] = values
    return result


def node_id(node):
    identity = {key: node.get(key, '') for key in ('domain', 'tls', 'uuid', 'path')}
    return 'node-' + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]


def nodes(config):
    return {node_id(node): node for node in config.get('v2node', []) if isinstance(node, dict) and node.get('domain')}


def make_outbound(node, tag):
    address = str(node.get('domain', ''))
    if address.startswith('['):
        match = re.fullmatch(r'\[([0-9a-fA-F:]+)\](?::([0-9]+))?', address)
    else:
        match = re.fullmatch(r'([A-Za-z0-9_.-]+)(?::([0-9]+))?', address)
    if not match:
        raise ValueError('Invalid node address')
    host, port = match.group(1), int(match.group(2) or 443)
    if not 1 <= port <= 65535:
        raise ValueError('Invalid node port')
    identifier = str(uuid.UUID(str(node.get('uuid', ''))))
    path = node.get('path', '')
    tls = node.get('tls') or host
    if not isinstance(path, str) or re.search(r'[\x00-\x1f\x7f]', path + tls):
        raise ValueError('Invalid node transport')
    stream = {'sockopt': {'mark': 255, 'tproxy': 'off', 'domainStrategy': 'UseIP'}}
    user = {'id': identifier, 'encryption': 'none'}
    if path:
        stream.update({'network': 'ws', 'security': 'tls',
                       'wsSettings': {'path': path, 'headers': {'Host': tls}},
                       'tlsSettings': {'serverName': tls, 'allowInsecure': False}})
    else:
        stream.update({'network': 'tcp', 'security': 'xtls',
                       'xtlsSettings': {'serverName': tls, 'allowInsecure': False, 'minVersion': '1.3'}})
        user['flow'] = 'xtls-rprx-direct'
    return {'tag': tag, 'protocol': 'vless', 'settings': {'vnext': [
        {'address': host, 'port': port, 'users': [user]}]}, 'streamSettings': stream}


def outbound_identity(outbound):
    """Compare connection parameters, rather than labels or legacy node hashes."""
    if outbound.get('protocol') != 'vless':
        return None
    try:
        target = outbound['settings']['vnext'][0]
        stream = outbound['streamSettings']
        host = str(target['address']).lower()
        security = stream.get('security', 'none')
        tls = stream.get('tlsSettings') or stream.get('xtlsSettings') or {}
        ws = stream.get('wsSettings', {})
        ws_host = str(ws.get('headers', {}).get('Host') or host).lower() if stream.get('network') == 'ws' else ''
        return (host, int(target['port']), str(uuid.UUID(target['users'][0]['id'])),
                stream.get('network', 'tcp'), security,
                str(tls.get('serverName') or host).lower(),
                ws.get('path', ''), ws_host)
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def recover_node_references(config, core, policies):
    """Recover a stale ID only when its compiled outbound identifies one node."""
    if not isinstance(policies, dict):
        return policies  # normalize() reports malformed policy maps.
    result = copy.deepcopy(policies)
    available = nodes(config)
    compiled = {out.get('tag'): out for out in core.get('outbounds', [])}
    for policy in result.values():
        if not isinstance(policy, dict) or policy.get('mode') != 'node' or policy.get('node') in available:
            continue
        identity = outbound_identity(compiled.get('svc-' + str(policy.get('node')), {}))
        if identity is None:
            continue
        matches = [key for key, node in available.items()
                   if outbound_identity(make_outbound(node, 'candidate')) == identity]
        if len(matches) == 1:
            policy['node'] = matches[0]
    return result


def replacement_policies(config, core, catalog, replacement):
    candidate = copy.deepcopy(config)
    candidate['v2node'] = replacement
    return normalize(recover_node_references(candidate, core, saved_policies(config)), catalog, candidate)


def selected_node(config, core):
    default = next((out for out in core.get('outbounds', []) if out.get('tag') == 'default'), {})
    identity = outbound_identity(default)
    if identity is not None:
        for index, node in enumerate(config.get('v2node', [])):
            if outbound_identity(make_outbound(node, 'candidate')) == identity:
                return index
    return None


def normalize(policies, catalog, config):
    if not isinstance(policies, dict) or set(policies) - {entry['id'] for entry in catalog['services']}:
        raise ValueError('Unknown service policy')
    available = nodes(config)
    result = {}
    assigned = set()
    for service in catalog['services']:
        policy = policies.get(service['id'], {'mode': 'default'})
        if not isinstance(policy, dict) or set(policy) - {'mode', 'node', 'devices'}:
            raise ValueError('Invalid policy fields')
        mode = policy.get('mode', 'default')
        if mode not in ('default', 'direct', 'node'):
            raise ValueError('Invalid routing mode')
        value = {'mode': mode}
        if mode == 'node':
            if policy.get('node') not in available:
                raise ValueError('Selected node is missing; choose an available node')
            value['node'] = policy['node']
        devices = policy.get('devices', [])
        if not isinstance(devices, list) or len(devices) > 32 or (devices and not service.get('devices')):
            raise ValueError('Invalid console address list')
        for address in devices:
            if not isinstance(address, str):
                raise ValueError('Console addresses must be strings')
            ip = ipaddress.ip_address(address)
            if ip.version != 4 or not any(ip in ipaddress.ip_network(net) for net in
                    ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')):
                raise ValueError('Console addresses must be private IPv4 addresses')
            if str(ip) in assigned:
                raise ValueError('A console address can belong to only one service')
            assigned.add(str(ip))
        if devices:
            value['devices'] = sorted(set(devices), key=ipaddress.ip_address)
        result[service['id']] = value
    return result


def saved_policies(config):
    if 'serviceRouting' in config:
        return config['serviceRouting'].get('policies', {})
    legacy = config.get('v2nodeDIV', {}).get('nodeNF', {})
    if legacy.get('display') != 'on':
        return {}
    for key, node in nodes(config).items():
        if node.get('uuid') == legacy.get('uuid') and node.get('path', '') == legacy.get('path', ''):
            return {'netflix': {'mode': 'node', 'node': key}}
    # A missing legacy node must be visible rather than silently changing its exit.
    return {'netflix': {'mode': 'node', 'node': 'legacy-node-missing'}}


def compile_routes(config, core, catalog, policies, site, geo):
    policies = normalize(recover_node_references(config, core, policies), catalog, config)
    result = copy.deepcopy(core)
    result['outbounds'] = [out for out in result['outbounds'] if not out.get('tag', '').startswith('svc-') and out.get('tag') != 'netflix']
    routing = result.setdefault('routing', {})
    old_rules = [rule for rule in routing.get('rules', []) if not rule.get('outboundTag', '').startswith('svc-') and rule.get('outboundTag') != 'netflix']
    prefix, remainder = [], []
    for rule in old_rules:
        if rule.get('inboundTag') == ['dnsFlow'] or rule.get('ip') == ['geoip:private']:
            prefix.append(rule)
        else:
            remainder.append(rule)
    default = next(out for out in result['outbounds'] if out['tag'] == 'default')
    default_copy = copy.deepcopy(default)
    default_copy['tag'] = 'svc-default'
    result['outbounds'] += [default_copy, {'tag': 'svc-direct', 'protocol': 'freedom',
                            'streamSettings': {'sockopt': {'mark': 255, 'tproxy': 'off'}}}]
    available = nodes(config)
    used_nodes, source_rules, domain_rules, ip_rules = set(), [], [], []
    forced_devices, dedicated_ips, dns_domains = set(), set(), set()
    for service in catalog['services']:
        policy = policies[service['id']]
        mode = policy['mode']
        if mode == 'default' and not policy.get('devices'):
            continue
        tag = 'svc-' + mode
        if mode == 'node':
            tag = 'svc-' + policy['node']
            if tag not in used_nodes:
                result['outbounds'].append(make_outbound(available[policy['node']], tag))
                used_nodes.add(tag)
        if policy.get('devices'):
            source_rules.append({'type': 'field', 'source': policy['devices'], 'outboundTag': tag})
            if mode != 'direct':
                forced_devices.update(policy['devices'])
        if mode == 'default':
            continue
        domains = ['domain:' + value for value in service.get('domains', [])]
        dns_domains.update(service.get('domains', []) if mode == 'node' else [])
        for category in service.get('geosite', []):
            if category in site:
                domains.append('geosite:' + category)
                if mode == 'node':
                    for field, kind, entry in site[category]:
                        if field == 2 and kind == 2:
                            data = dict((key, value) for key, _, value in fields(entry))
                            if data.get(1) in (2, 3) and isinstance(data.get(2), bytes):
                                domain = data[2].decode('ascii')
                                if re.fullmatch(r'[A-Za-z0-9_.-]+', domain):
                                    dns_domains.add(domain)
        domain_rules.append({'type': 'field', 'domain': sorted(set(domains)), 'outboundTag': tag})
        for category in service.get('geoip', []):
            if category not in geo:
                continue
            ip_rules.append({'type': 'field', 'ip': ['geoip:' + category], 'outboundTag': tag})
            if mode != 'node':
                continue
            for field, kind, entry in geo[category]:
                if field == 2 and kind == 2:
                    data = dict((key, value) for key, _, value in fields(entry))
                    if isinstance(data.get(1), bytes) and len(data[1]) == 4:
                        dedicated_ips.add(str(ipaddress.ip_network((socket_address(data[1]), data.get(2, 0)))))
    # Domain rules precede IP rules; fields in one rule would mean AND, not OR.
    routing['rules'] = prefix + source_rules + domain_rules + ip_rules + remainder
    return result, policies, forced_devices, dedicated_ips, dns_domains


def socket_address(packed):
    return str(ipaddress.ip_address(packed))


def atomic_write(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
        if path.exists():
            info = path.stat()
            os.chmod(temporary, info.st_mode & 0o777)
            os.chown(temporary, info.st_uid, info.st_gid)
        else:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def status(root, catalog, config):
    manifest = root/'rules/manifest.json'
    progress = root/'rule-update-status.json'
    core_file = root/'vtrui/config.json'
    core = json.loads(core_file.read_text()) if core_file.exists() else {}
    return {'categories': catalog['categories'], 'services': catalog['services'],
            'nodes': [{'id': key, 'name': str(node.get('name') or node['domain'])} for key, node in nodes(config).items()],
            'policies': recover_node_references(config, core, saved_policies(config)),
            'lastApplied': config.get('serviceRouting', {}).get('lastApplied'),
            'udpEnabled': config.get('address', {}).get('udp') == 'on',
            'rules': json.loads(manifest.read_text()) if manifest.exists() else {},
            'ruleUpdate': json.loads(progress.read_text()) if progress.exists() else {},
            'schedule': '每天 04:00 自动更新 GeoIP / GeoSite / 中国路由及广告规则'}


def validate_core(root, core):
    core_file = root/'vtrui/config.json'
    fd, candidate = tempfile.mkstemp(prefix='.services-', suffix='.json', dir=core_file.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(core, output)
        # Never return core output: it may contain private node parameters.
        check = subprocess.run([str(root/'vtrui/vtrui'), '-test', '-config', candidate],
                               cwd=core_file.parent, capture_output=True, timeout=30)
        if check.returncode:
            raise ValueError('Core rejected the routing configuration; previous configuration preserved')
    finally:
        Path(candidate).unlink(missing_ok=True)


def proxy_ready(config):
    transports = ['tcp'] + (['udp'] if config.get('address', {}).get('udp') == 'on' else [])
    for _ in range(50):
        if subprocess.run(['systemctl', 'is-active', '--quiet', 'vtrui'], capture_output=True,
                          timeout=5).returncode == 0:
            if all(subprocess.run(['ss', '-H', '-lnt' if protocol == 'tcp' else '-lnu',
                                   '( sport = :9896 )'], capture_output=True, timeout=5).stdout.strip()
                   for protocol in transports):
                return True
        time.sleep(0.1)
    return False


class SwitchFailure(ValueError):
    def __init__(self, recovered):
        self.recovered = recovered
        super().__init__('Node switch failed')


def switch_node(root, config, index):
    if not isinstance(index, int) or not 0 <= index < len(config.get('v2node', [])):
        raise ValueError('Invalid node index')
    core_file, config_file = root/'vtrui/config.json', root/'0conf'
    core = json.loads(core_file.read_text())
    if sum(out.get('tag') == 'default' for out in core.get('outbounds', [])) != 1:
        raise ValueError('Missing or duplicate default outbound')
    node = config['v2node'][index]
    default = make_outbound(node, 'default')
    candidate = copy.deepcopy(core)
    for position, outbound in enumerate(candidate['outbounds']):
        if outbound.get('tag') in ('default', 'svc-default'):
            candidate['outbounds'][position] = dict(copy.deepcopy(default), tag=outbound['tag'])
    # Default selection must not recompile unrelated pinned service policies.
    updated = copy.deepcopy(config)
    target = default['settings']['vnext'][0]
    updated.setdefault('update', {})['v2node'] = {
        'domain': target['address'], 'port': str(target['port']),
        'tls': node.get('tls') or target['address'],
        'uuid': target['users'][0]['id'], 'path': node.get('path', '')}
    repaired = 0
    if 'serviceRouting' in config:
        original = saved_policies(config)
        recovered = recover_node_references(config, core, original)
        replacements = {}
        for service, policy in recovered.items():
            if isinstance(policy, dict) and policy.get('mode') == 'node' and policy.get('node') != original[service].get('node'):
                replacements['svc-' + original[service]['node']] = 'svc-' + policy['node']
                repaired += 1
        # These references were matched by endpoint, UUID, SNI and transport.
        # Rename only their tags; never replace a pin merely by matching its host.
        for old_tag, new_tag in replacements.items():
            old = next(out for out in candidate['outbounds'] if out.get('tag') == old_tag)
            existing = next((out for out in candidate['outbounds'] if out.get('tag') == new_tag), None)
            if existing is not None:
                if outbound_identity(existing) != outbound_identity(old):
                    raise ValueError('Conflicting service node identity')
                candidate['outbounds'].remove(old)
            else:
                old['tag'] = new_tag
            for rule in candidate.get('routing', {}).get('rules', []):
                if rule.get('outboundTag') == old_tag:
                    rule['outboundTag'] = new_tag
        if repaired:
            updated['serviceRouting']['policies'] = recovered
    validate_core(root, candidate)
    originals = {path: path.read_bytes() for path in (core_file, config_file)}
    was_active = subprocess.run(['systemctl', 'is-active', '--quiet', 'vtrui'],
                                capture_output=True, timeout=5).returncode == 0
    try:
        atomic_write(core_file, json.dumps(candidate, indent=2).encode() + b'\n')
        atomic_write(config_file, json.dumps(updated, indent=2).encode() + b'\n')
        subprocess.run(['systemctl', 'restart', 'vtrui'], check=True, capture_output=True, timeout=90)
        if not proxy_ready(updated):
            raise ValueError('Proxy listener did not become ready')
    except BaseException:
        recovered = True
        for path, data in originals.items():
            try:
                atomic_write(path, data)
            except OSError:
                recovered = False
        try:
            subprocess.run(['systemctl', 'restart' if was_active else 'stop', 'vtrui'],
                           check=True, capture_output=True, timeout=90)
            if was_active and not proxy_ready(config):
                recovered = False
        except (OSError, ValueError, subprocess.SubprocessError):
            recovered = False
        raise SwitchFailure(recovered) from None
    return {'saved': True, 'nodeIndex': index, 'recoveredPolicies': repaired}


def apply(root, catalog, config, policies, restart=True):
    core_file = root/'vtrui/config.json'
    site, geo = geodata(root/'vtrui/geosite.dat'), geodata(root/'vtrui/geoip.dat')
    core, policies, devices, networks, domains = compile_routes(config, json.loads(core_file.read_text()), catalog, policies, site, geo)
    dns_file = Path('/etc/dnsmasq.d/90-de-gwd-services.conf') if root == Path('/opt/de_GWD') else root/'services-dnsmasq.conf'
    validate_core(root, core)
    updated = copy.deepcopy(config)
    updated['serviceRouting'] = {'schema': 1, 'policies': policies, 'lastApplied': datetime.now(timezone.utc).isoformat()}
    updated.setdefault('v2nodeDIV', {}).setdefault('nodeNF', {})['display'] = 'off'
    changes = {core_file: json.dumps(core, indent=2).encode() + b'\n',
               root/'0conf': json.dumps(updated, indent=2).encode() + b'\n',
               root/'IPconsoleSET': ''.join('add gwdConsole ' + ip + '\n' for ip in sorted(devices)).encode(),
               root/'IPserviceSET': ''.join('add gwdService ' + ip + '\n' for ip in sorted(networks)).encode(),
               dns_file: ''.join('ipset=/' + domain + '/gwdService\n' for domain in sorted(domains)).encode()}
    backups = {path: (path.read_bytes() if path.exists() else None) for path in changes}
    active = []
    if restart:
        active = [unit for unit in ('vtrui', 'iptables-proxy', 'pihole-FTL') if subprocess.run(
            ['systemctl', 'is-active', '--quiet', unit], capture_output=True).returncode == 0]
    try:
        for path, data in changes.items():
            atomic_write(path, data)
        if restart:
            subprocess.run(['ipset', 'create', 'gwdService', 'hash:net', 'family', 'inet', '-exist'], check=True, capture_output=True)
            subprocess.run(['ipset', 'create', 'gwdConsole', 'hash:ip', 'family', 'inet', '-exist'], check=True, capture_output=True)
            if root == Path('/opt/de_GWD'):
                subprocess.run(['pihole-FTL', 'dnsmasq-test'], check=True, capture_output=True, timeout=20)
            for unit in active:
                subprocess.run(['systemctl', 'restart', unit], check=True, capture_output=True, timeout=90)
                subprocess.run(['systemctl', 'is-active', '--quiet', unit], check=True, capture_output=True)
    except Exception:
        for path, data in backups.items():
            if data is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, data)
        for unit in active:
            subprocess.run(['systemctl', 'restart', unit], capture_output=True, timeout=90)
        raise ValueError('Applying routes failed; previous files restored') from None
    return {'saved': True, 'serviceCount': len(policies), 'consoleCount': len(devices),
            'dedicatedNetworks': len(networks), 'dnsDomains': len(domains)}


@contextmanager
def deployment_lock(root):
    # ui-updateRules locks this same directory while replacing data and policy.
    descriptor = os.open(root, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('status', 'apply', 'reapply', 'switch-node', 'selected-node'))
    parser.add_argument('--root', default=os.environ.get('DE_GWD_ROOT', '/opt/de_GWD'))
    parser.add_argument('--no-restart', action='store_true')
    parser.add_argument('--index', type=int)
    args = parser.parse_args()
    if args.operation == 'switch-node':
        def interrupted(signum, frame):
            raise InterruptedError('Node switch interrupted')
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGHUP, interrupted)
    root = Path(args.root)
    try:
        if args.operation == 'selected-node':
            index = selected_node(json.loads(root.joinpath('0conf').read_text()),
                                  json.loads(root.joinpath('vtrui/config.json').read_text()))
            print('' if index is None else index)
            return 0
        catalog = json.loads(root.joinpath('service-catalog.json').read_text()) if args.operation != 'switch-node' else None
        if args.operation == 'status':
            result = status(root, catalog, json.loads(root.joinpath('0conf').read_text()))
        else:
            if os.geteuid() != 0:
                raise ValueError('Applying service routes requires root')
            lock_path = Path('/run/lock/de-gwd-services.lock') if root == Path('/opt/de_GWD') else root/'.service-routing.lock'
            with lock_path.open('w') as lock, deployment_lock(root):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                config = json.loads(root.joinpath('0conf').read_text())
                if args.operation == 'switch-node':
                    result = switch_node(root, config, args.index)
                elif args.operation == 'apply':
                    payload = sys.stdin.buffer.read(98305)
                    if len(payload) > 98304:
                        raise ValueError('Request too large')
                    policies = json.loads(payload)
                else:
                    policies = saved_policies(config)
                if args.operation != 'switch-node':
                    result = apply(root, catalog, config, policies, restart=not args.no_restart)
        print(json.dumps(result, ensure_ascii=False))
    except SwitchFailure as error:
        message = ('节点切换失败，已恢复原配置和代理服务。' if error.recovered else
                   '节点切换失败，恢复未完成，请检查代理服务和配置。')
        print(json.dumps({'error': message}, ensure_ascii=False))
        return 1
    except (OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError):
        # Deliberately generic: do not log submitted UUIDs, paths or credentials.
        message = ('节点切换未应用，请检查节点参数、配置和服务状态。' if args.operation == 'switch-node' else
                   '分流配置未应用，请检查节点、主机 IP、规则库和服务状态。原配置已保留。')
        print(json.dumps({'error': message}, ensure_ascii=False))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
