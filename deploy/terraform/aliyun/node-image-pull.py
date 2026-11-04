#!/usr/bin/env python3
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0

import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import ssl
import subprocess
import tempfile
import time
import socket
import urllib.error
import urllib.parse
import urllib.request


BASE = Path('/etc/akernel/image-pull')
CUSTOM = Path('/etc/kubernetes/kubelet-customized-args.conf')
UPSTREAM = BASE / 'kubelet-resolv.conf'
KUBELET_CONF = Path('/etc/kubernetes/kubelet.conf')
CONTAINERD_CONFIG = Path('/etc/containerd/config.toml')
HOST_RESOLV = Path('/etc/resolv.conf')
INSTANCE_ID = Path('/var/lib/cloud/data/instance-id')
PROFILE_LABEL = 'akernel.io/image-pull-profile'
READY_LABEL = 'akernel.io/image-pull-ready'
POOL_LABEL = 'akernel.io/image-pull-pool'
PROFILE = 'ack-dragonfly-v1'
TIMEOUT = 600


class Pending(RuntimeError):
    pass


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def available(ok, message):
    if not ok:
        raise Pending(message)


def sha(content):
    return hashlib.sha256(content).hexdigest()


def retry(action, seconds=TIMEOUT):
    deadline = time.monotonic() + seconds
    while True:
        try:
            return action()
        except (Pending, OSError, subprocess.TimeoutExpired) as error:
            remaining = deadline - time.monotonic()
            require(remaining > 0, 'bounded readiness timeout')
            print('ACK image-pull retry: ' + type(error).__name__, flush=True)
            time.sleep(min(5, remaining))


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()


def atomic_write(path, content, mode=0o600):
    require(not any(p.is_symlink() for p in [path] + list(path.parents)),
            'symlink configuration path')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.bootstrap-', dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def owned_write(path, content, original=None):
    require(not any(p.is_symlink() for p in [path] + list(path.parents)), 'symlink configuration path')
    if path.exists():
        require(path.read_bytes() in [content, original],
                'foreign configuration: ' + str(path))
    if not path.exists() or path.read_bytes() != content:
        atomic_write(path, content, 0o644)


def run(arguments, timeout=30):
    result = subprocess.run(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout, env=dict(os.environ, PATH='/usr/sbin:/usr/bin:/sbin:/bin'))
    available(result.returncode == 0, 'command failed: ' + arguments[0])
    return result.stdout.decode().strip()


def scalar(text, key):
    matches = re.findall(r'(?m)^\s*' + re.escape(key) + r':\s*(\S+)\s*$', text)
    require(len(matches) == 1, 'missing or ambiguous configuration field: ' + key)
    return matches[0].strip('\"\'')


def argument(arguments, key, default=None):
    values = []
    for index, item in enumerate(arguments):
        if item.startswith(key + '='):
            values.append(item.split('=', 1)[1])
        elif item == key:
            require(index + 1 < len(arguments), 'incomplete kubelet argument')
            values.append(arguments[index + 1])
    require(len(values) <= 1, 'duplicate kubelet argument: ' + key)
    return values[0] if values else default


def kubelet_arguments():
    processes = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / 'comm').read_text().strip() == 'kubelet':
                processes.append([x.decode() for x in (entry / 'cmdline').read_bytes().split(b'\0') if x])
        except OSError:
            continue
    available(len(processes) == 1, 'one kubelet required')
    return processes[0]


def kubelet_dns(text):
    if text.lstrip().startswith('{'):
        config = json.loads(text)
        return config.get('clusterDNS', []), config.get('resolvConf', '/etc/resolv.conf')
    match = re.search(r'(?m)^clusterDNS:\s*(\[[^\n]*\])?\s*\n((?:[ \t]*-[^\n]*\n)*)', text)
    if match is None:
        addresses = []
    elif match.group(1):
        addresses = [x.strip().strip('\"\'') for x in match.group(1)[1:-1].split(',') if x.strip()]
    else:
        addresses = [x.strip().strip('\"\'') for x in re.findall(r'(?m)^\s*-\s*(\S+)\s*$', match.group(2))]
    resolver = re.findall(r'(?m)^resolvConf:\s*(\S+)\s*$', text)
    require(len(resolver) <= 1, 'ambiguous resolvConf')
    return addresses, resolver[0].strip('\"\'') if resolver else '/etc/resolv.conf'


def upstream_addresses(text, cluster_dns):
    addresses = re.findall(r'(?m)^\s*nameserver\s+(\S+)\s*(?:#.*)?$', text)
    result = []
    for value in addresses:
        address = ipaddress.ip_address(value)
        require(address.version == 4 and not address.is_loopback and not address.is_unspecified,
                'IPv4 upstream required')
        require(value not in cluster_dns, 'upstream loops to CoreDNS')
        if value not in result:
            result.append(value)
    require(bool(result) and len(result) <= 2, 'one/two upstreams required')
    return result


def dns_options(text):
    return [value for line in re.findall(r'(?m)^\s*options\s+([^#;\n]+)', text) for value in line.split()]


def nm_values(text):
    return [value for value in re.split(r'[,;\s]+', text) if value]


def customized_args(text):
    matches = list(re.finditer(r'(?m)^[ \t]*(?:export[ \t]+)?KUBELET_CUSTOMIZED_ARGS[ \t]*=.*$', text))
    require(len(matches) <= 1, 'duplicate custom args')
    arguments = []
    if matches:
        line = matches[0].group().strip()
        if line.startswith('export '):
            line = line[7:]
        tokens = shlex.split(line, comments=True)
        require(len(tokens) == 1 and tokens[0].startswith('KUBELET_CUSTOMIZED_ARGS='),
                'nonliteral custom args')
        arguments = shlex.split(tokens[0].split('=', 1)[1])
        require(not any('$' in item or '`' in item for item in arguments),
                'custom args contain expansion')
    retained = []
    index = 0
    while index < len(arguments):
        item = arguments[index]
        if item == '--resolv-conf':
            require(index + 1 < len(arguments), 'incomplete resolv-conf flag')
            index += 2
            continue
        if not item.startswith('--resolv-conf='):
            retained.append(item)
        index += 1
    retained.append('--resolv-conf=' + str(UPSTREAM))
    replacement = 'KUBELET_CUSTOMIZED_ARGS=' + shlex.quote(' '.join(shlex.quote(x) for x in retained))
    if matches:
        return (text[:matches[0].start()] + replacement + text[matches[0].end():]).encode()
    return (text + ('' if not text or text.endswith('\n') else '\n') + replacement + '\n').encode()


def hosts_roots(text, version=None):
    section = ''
    paths = []
    local_pull = False
    for line in text.splitlines():
        header = re.fullmatch(r'\s*\[([^\[\]]+)\]\s*(?:#.*)?', line)
        if header:
            section = header.group(1).replace('\"', '').replace("'", '')
        if section == 'plugins.io.containerd.cri.v1.images':
            local_pull = local_pull or re.fullmatch(r'\s*use_local_image_pull\s*=\s*true\s*(?:#.*)?', line) is not None
        if section not in ('plugins.io.containerd.grpc.v1.cri.registry',
                           'plugins.io.containerd.cri.v1.images.registry'):
            continue
        match = re.fullmatch(r'\s*config_path\s*=\s*[\"\']([^\"\']+)[\"\']\s*(?:#.*)?', line)
        if match:
            paths.append((section, match.group(1)))
    require(len(paths) == 1, 'ambiguous CRI config_path')
    section, path = paths[0]
    roots = path.split(':')
    require(bool(roots) and all(root in ('/etc/containerd/cert.d', '/etc/containerd/certs.d') for root in roots),
            'unsupported CRI config_path')
    compatible_fallback = version is not None and version[:2] == (2, 3) and version >= (2, 3, 4)
    require(len(roots) == 1 or section == 'plugins.io.containerd.grpc.v1.cri.registry' or local_pull or compatible_fallback,
            'unsupported multi-root Transfer Service')
    return [Path(root) for root in roots]


def socket_path(endpoint):
    parsed = urllib.parse.urlsplit(endpoint)
    require(parsed.scheme in ('', 'unix') and not parsed.netloc and not parsed.query and not parsed.fragment,
            'local Unix CRI endpoint required')
    path = Path(parsed.path)
    require(path.is_absolute(), 'CRI socket path must be absolute')
    return str(path.resolve())


def cri_socket(dump, arguments):
    endpoint = argument(arguments, '--container-runtime-endpoint')
    if endpoint is None:
        config = Path(argument(arguments, '--config', '/etc/kubernetes/config')).read_text()
        endpoint = json.loads(config).get('containerRuntimeEndpoint') if config.lstrip().startswith('{') else scalar(config, 'containerRuntimeEndpoint')
    require(bool(endpoint), 'kubelet CRI endpoint is missing')
    section, addresses = '', []
    for line in dump.splitlines():
        header = re.fullmatch(r'\s*\[([^\[\]]+)\]\s*(?:#.*)?', line)
        if header:
            section = header.group(1).strip().strip('"\'')
        match = re.fullmatch(r'\s*address\s*=\s*["\']([^"\']+)["\']\s*(?:#.*)?', line)
        if section == 'grpc' and match:
            addresses.append(match.group(1))
    require(len(addresses) == 1 and socket_path(endpoint) == socket_path(addresses[0]),
            'CRI endpoint does not match daemon')
    return socket_path(endpoint)


def active_cri():
    pid = run(['systemctl', 'show', '--property=MainPID', '--value', 'containerd.service'])
    require(pid.isdigit() and int(pid) > 1, 'no containerd MainPID')
    proc = Path('/proc') / pid
    available((proc / 'comm').read_text().strip() == 'containerd', 'containerd process pending')
    arguments = [x.decode() for x in (proc / 'cmdline').read_bytes().split(b'\0') if x]
    long_path, short_path = argument(arguments, '--config'), argument(arguments, '-c')
    require(not (long_path and short_path), 'ambiguous daemon config')
    path = Path(long_path or short_path or str(CONTAINERD_CONFIG))
    require(path.is_absolute() and not path.is_symlink(), 'unsafe daemon config')
    executable = str(proc / 'exe')
    version_text = run([executable, '--version'])
    matched = re.search(r'\bv?(\d+)\.(\d+)\.(\d+)\b', version_text)
    require(matched is not None, 'unknown daemon version')
    version = tuple(int(x) for x in matched.groups())
    dump = run([executable, '--config', str(path), 'config', 'dump'])
    endpoint = cri_socket(dump, kubelet_arguments())
    owned = set()
    for fd in (proc / 'fd').iterdir():
        try:
            match = re.fullmatch(r'socket:\[(\d+)\]', os.readlink(str(fd)))
            if match: owned.add(match.group(1))
        except FileNotFoundError:
            continue
    sockets = [line.split() for line in (proc / 'net/unix').read_text().splitlines()[1:]]
    require(any(len(row) >= 8 and row[6] in owned and socket_path(row[7]) == endpoint
                for row in sockets if len(row) >= 8 and row[7].startswith('/')),
            'daemon does not own CRI socket')
    roots = hosts_roots(dump, version)
    return dict(main_path=str(path), main_sha256=sha(path.read_bytes()),
                effective_sha256=sha(dump.encode()), grpc_address=endpoint, roots=[str(p) for p in roots])


def mirror_config(registry, domain):
    require(len(registry) <= 253 and re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*', registry) is not None,
            'invalid registry hostname')
    require(re.fullmatch(r'dragonfly-seed-client\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.svc\.cluster\.local', domain)
            is not None, 'invalid Dragonfly Service domain')
    endpoint = 'http://' + domain + ':4001'
    return ('# Managed by AKernel ACK image-pull bootstrap\n'
            'server = "https://' + registry + '"\n\n'
            '[host."' + endpoint + '"]\n'
            '  capabilities = ["pull"]\n'
            '  [host."' + endpoint + '".header]\n'
            '    X-Dragonfly-Registry = ["https://' + registry + '"]\n').encode()


class Bootstrap:
    def __init__(self, profile):
        fields = {'cluster_id', 'region', 'registry', 'domain', 'service_cidr', 'helper_sha256', 'pool_marker'}
        require(not set(profile) - fields and fields - {'pool_marker'} <= set(profile), 'invalid bootstrap profile')
        require(re.fullmatch(r'[a-zA-Z0-9-]{1,128}', profile['cluster_id']) and
                re.fullmatch(r'[a-z0-9-]{1,63}', profile['region']), 'invalid ACK scope')
        require(re.fullmatch(r'[0-9a-f]{64}', profile['helper_sha256']), 'invalid helper digest')
        require(ipaddress.ip_network(profile['service_cidr']).version == 4, 'IPv4 Service CIDR required')
        if 'pool_marker' in profile:
            require(re.fullmatch(r'[a-zA-Z0-9]([-_.a-zA-Z0-9]{0,61}[a-zA-Z0-9])?', profile['pool_marker']), 'invalid pool marker')
        self.profile, self.identity = profile, None
        self.mirror = mirror_config(profile['registry'], profile['domain'])

    def api(self, method='GET', patch=None, dry_run=False):
        config = KUBELET_CONF.read_text()
        server = scalar(config, 'server').rstrip('/')
        parsed = urllib.parse.urlsplit(server)
        require(parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment and not parsed.path, 'invalid kubelet API endpoint')
        if re.search(r'(?m)^\s*certificate-authority-data:', config):
            ca = base64.b64decode(scalar(config, 'certificate-authority-data'), validate=True).decode()
        else:
            ca = Path(scalar(config, 'certificate-authority')).read_text()
        cert = scalar(config, 'client-certificate')
        require(cert == scalar(config, 'client-key') == '/var/lib/kubelet/pki/kubelet-client-current.pem'
                and str(Path(cert).resolve()).startswith('/var/lib/kubelet/pki/'), 'unexpected kubelet certificate')
        subject = ssl._ssl._test_decode_cert(str(Path(cert).resolve()))['subject']
        names = [v for group in subject for k, v in group if k == 'commonName']
        require(len(names) == 1 and names[0].startswith('system:node:'), 'invalid kubelet certificate CN')
        name = names[0][len('system:node:'):]
        require(re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,251}[a-z0-9]|[a-z0-9]', name), 'invalid own Node name')
        self.identity = dict(node=name, server=server, ca_sha256=sha(ssl.PEM_cert_to_DER_cert(ca)))
        context = ssl.create_default_context(cadata=ca)
        context.load_cert_chain(cert)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))
        require(not dry_run or method == 'PATCH', 'invalid dry-run method')
        url = server + '/api/v1/nodes/' + urllib.parse.quote(name, safe='') + ('?dryRun=All' if dry_run else '')
        request = urllib.request.Request(url, data=canonical(patch) if patch is not None else None,
                                        headers={'Content-Type': 'application/json-patch+json'}, method=method)
        try:
            with opener.open(request, timeout=12) as response:
                node = json.load(response)
        except urllib.error.HTTPError as error:
            if error.code in (404, 409, 422, 429, 500, 502, 503, 504):
                raise Pending('own Node API pending/conflict ' + str(error.code)) from None
            raise RuntimeError('own Node API denied/status ' + str(error.code)) from None
        require(node['metadata']['name'] == name, 'own Node response mismatch')
        return node

    def guard(self):
        node = self.api(); metadata = node['metadata']; labels = metadata.get('labels', {})
        instance = INSTANCE_ID.read_text().strip()
        require(re.fullmatch(r'i-[a-z0-9]+', instance), 'invalid instance identity')
        require(labels.get('ack.aliyun.com') == self.profile['cluster_id'], 'ACK cluster mismatch')
        require(labels.get(PROFILE_LABEL) == PROFILE and labels.get(READY_LABEL) in ('false', 'true'), 'bootstrap registration mismatch')
        require(labels.get('node-role') not in ('dragonfly-seed', 'dragonfly-server'), 'Dragonfly pool excluded')
        if 'pool_marker' in self.profile:
            require(labels.get(POOL_LABEL) == self.profile['pool_marker'], 'logical pool marker mismatch')
        pool = labels.get('alibabacloud.com/nodepool-id')
        require(pool and node['spec'].get('providerID') == self.profile['region'] + '.' + instance
                and not metadata.get('deletionTimestamp'), 'wrong/deleting ECS node')
        require(self.identity is not None and self.identity['node'] == metadata['name'], 'missing certificate identity')
        available(any(x.get('type') == 'Ready' and x.get('status') == 'True' for x in node.get('status', {}).get('conditions', [])), 'own Node not Ready')
        binding = dict(node=metadata['name'], uid=metadata['uid'], instance=instance, pool=pool,
                       identity=self.identity, profile_sha256=sha(canonical(self.profile)))
        path = BASE / 'binding.json'
        if path.exists():
            require(json.loads(path.read_text()) == binding, 'local node binding changed')
        else:
            atomic_write(path, canonical(binding))
        return node

    def patch(self, node):
        m = node['metadata']
        return [dict(op='test', path='/metadata/uid', value=m['uid']),
                dict(op='test', path='/metadata/resourceVersion', value=m['resourceVersion']),
                dict(op='test', path='/metadata/labels', value=m['labels']),
                dict(op='test', path='/metadata/labels/akernel.io~1image-pull-profile', value=PROFILE),
                dict(op='test', path='/metadata/labels/akernel.io~1image-pull-ready', value='false'),
                dict(op='replace', path='/metadata/labels/akernel.io~1image-pull-ready', value='true')]

    def load_baseline(self):
        self.baseline = json.loads((BASE / 'baseline.json').read_text())
        require(self.baseline['binding'] == json.loads((BASE / 'binding.json').read_text()), 'baseline identity changed')
        self.hosts = Path(self.baseline['hosts'])
        self.cluster_dns = self.baseline['cluster_dns']

    def prepare(self):
        arguments = kubelet_arguments()
        dns, resolver = kubelet_dns(Path(argument(arguments, '--config', '/etc/kubernetes/config')).read_text())
        override = argument(arguments, '--cluster-dns')
        if override is not None:
            dns = override.split(',')
        require(len(dns) == 1 and ipaddress.ip_address(dns[0]) in ipaddress.ip_network(self.profile['service_cidr']), 'one CoreDNS IPv4 required')
        cri = active_cri(); roots = [Path(p) for p in cri['roots']]
        hosts = roots[0] / self.profile['registry'] / 'hosts.toml'
        for root in roots[1:]:
            require(not (root / self.profile['registry']).exists(), 'competing registry directory')
        require(run(['systemctl', 'is-active', 'NetworkManager']) == 'active', 'inactive NetworkManager')
        routes = json.loads(run(['ip', '-j', 'route', 'show', 'default']))
        require(len(routes) == 1, 'one default route required')
        device = routes[0]['dev']; connection = run(['nmcli', '-g', 'GENERAL.CON-UUID', 'device', 'show', device])
        require(re.fullmatch(r'[a-f0-9-]{36}', connection), 'invalid NM connection')
        if (BASE / 'baseline.json').exists():
            self.load_baseline()
            require((self.baseline['device'], self.baseline['connection'], self.baseline['cri'], str(self.hosts), self.cluster_dns)
                    == (device, connection, cri, str(hosts), dns[0]), 'network/CRI changed')
            return
        require(not hosts.exists() and not hosts.is_symlink() and not UPSTREAM.exists() and not CUSTOM.is_symlink(), 'foreign managed configuration')
        original = Path(argument(arguments, '--resolv-conf', resolver)).read_text()
        baseline = dict(binding=json.loads((BASE / 'binding.json').read_text()), device=device, connection=connection,
                        cri=cri, hosts=str(hosts), cluster_dns=dns[0], upstream=original,
                        upstream_ips=upstream_addresses(original, dns), host_options=dns_options(HOST_RESOLV.read_text()),
                        custom=CUSTOM.read_text() if CUSTOM.exists() else None,
                        custom_mode=(CUSTOM.stat().st_mode & 0o777) if CUSTOM.exists() else 0o644,
                        nm_dns=run(['nmcli', '-g', 'ipv4.dns', 'connection', 'show', connection]),
                        nm_dns_options=run(['nmcli', '--escape', 'no', '-g', 'ipv4.dns-options', 'connection', 'show', connection]),
                        nm_ignore_auto_dns=run(['nmcli', '-g', 'ipv4.ignore-auto-dns', 'connection', 'show', connection]))
        require('no-reload' not in baseline['host_options'], 'unsupported DNS no-reload')
        atomic_write(BASE / 'baseline.json', canonical(baseline)); self.load_baseline()

    def dns_state(self):
        b = self.baseline; connection = b['connection']
        require(run(['nmcli', '-g', 'GENERAL.CON-UUID', 'device', 'show', b['device']]) == connection, 'active NM connection changed')
        return (nm_values(run(['nmcli', '-g', 'ipv4.dns', 'connection', 'show', connection])),
                run(['nmcli', '-g', 'ipv4.ignore-auto-dns', 'connection', 'show', connection]),
                nm_values(run(['nmcli', '--escape', 'no', '-g', 'ipv4.dns-options', 'connection', 'show', connection])))

    def configure(self):
        b = self.baseline; wanted = [self.cluster_dns] + b['upstream_ips']
        options = [x for x in b['host_options'] if x != 'rotate']; dns, ignore, current_options = self.dns_state()
        require(dns in (nm_values(b['nm_dns']), wanted) and ignore in (b['nm_ignore_auto_dns'], 'yes')
                and current_options in (nm_values(b['nm_dns_options']), options), 'foreign NM DNS settings')
        self.guard(); owned_write(UPSTREAM, b['upstream'].encode())
        custom = customized_args(b['custom'] or '')
        owned_write(CUSTOM, custom, b['custom'].encode() if b['custom'] is not None else None)
        os.chmod(str(CUSTOM), b['custom_mode'])
        if argument(kubelet_arguments(), '--resolv-conf') != str(UPSTREAM):
            intent = BASE / 'kubelet-restart.json'
            if not intent.exists():
                atomic_write(intent, canonical(dict(custom_sha256=sha(custom))))
                try:
                    run(['systemctl', 'restart', 'kubelet'], timeout=60)
                except Pending:
                    intent.unlink()
                    raise
            available(argument(kubelet_arguments(), '--resolv-conf') == str(UPSTREAM), 'resolver restart pending')
        self.guard()
        if (dns, ignore, current_options) != (wanted, 'yes', options):
            run(['nmcli', 'connection', 'modify', b['connection'], 'ipv4.ignore-auto-dns', 'yes',
                 'ipv4.dns', ','.join(wanted), 'ipv4.dns-options', ','.join(options) or ' '])
        effective = HOST_RESOLV.read_text()
        actual = re.findall(r'(?m)^\s*nameserver\s+(\S+)\s*(?:#.*)?$', effective)
        if actual != wanted or dns_options(effective) != options:
            run(['nmcli', 'device', 'reapply', b['device']])
        effective = HOST_RESOLV.read_text()
        available(re.findall(r'(?m)^\s*nameserver\s+(\S+)\s*(?:#.*)?$', effective) == wanted
                  and dns_options(effective) == options, 'host DNS apply pending')
        require('rotate' not in dns_options(effective) and 'no-reload' not in dns_options(effective), 'invalid host DNS options')
        require(active_cri() == b['cri'], 'CRI configuration changed')
        owned_write(self.hosts, self.mirror)
        time.sleep(6)  # Allow the existing containerd resolver's refresh window; no Service lookup.
        atomic_write(BASE / 'configured.json', canonical(dict(binding=b['binding'], mirror_sha256=sha(self.mirror))))

    def verify(self):
        self.load_baseline(); b = self.baseline
        receipt = json.loads((BASE / 'configured.json').read_text())
        require(receipt == dict(binding=b['binding'], mirror_sha256=sha(self.mirror)), 'configuration receipt changed')
        require(active_cri() == b['cri'], 'CRI configuration changed')
        for path, expected in ((UPSTREAM, b['upstream'].encode()), (CUSTOM, customized_args(b['custom'] or '')), (self.hosts, self.mirror)):
            require(not any(p.is_symlink() for p in [path] + list(path.parents))
                    and path.read_bytes() == expected, 'owned configuration changed: ' + str(path))
        require(argument(kubelet_arguments(), '--resolv-conf') == str(UPSTREAM), 'kubelet resolver changed')
        wanted = [self.cluster_dns] + b['upstream_ips']; options = [x for x in b['host_options'] if x != 'rotate']
        require(self.dns_state() == (wanted, 'yes', options), 'owned NM DNS changed')
        text = HOST_RESOLV.read_text()
        require(re.findall(r'(?m)^\s*nameserver\s+(\S+)\s*(?:#.*)?$', text) == wanted and dns_options(text) == options, 'host DNS changed')
        available(bool(socket.getaddrinfo(self.profile['registry'], 443, socket.AF_INET, socket.SOCK_STREAM)), 'origin DNS unavailable')

    def release(self):
        self.verify(); node = self.guard(); m = node['metadata']
        if m['labels'][READY_LABEL] != 'true':
            self.api('PATCH', self.patch(node))
            available(self.guard()['metadata']['labels'][READY_LABEL] == 'true', 'ready readback pending')
        atomic_write(BASE / 'status.json', canonical(dict(state='configured', node=m['name'], acr_fallback=True)))

    def start(self):
        node = self.guard()
        if node['metadata']['labels'][READY_LABEL] == 'true':
            self.release(); return
        if not (BASE / 'configured.json').exists():
            result = self.api('PATCH', self.patch(node), dry_run=True)
            expected = dict(node['metadata']['labels']); expected[READY_LABEL] = 'true'
            require(result['metadata']['uid'] == node['metadata']['uid'] and result['metadata']['labels'] == expected, 'dry-run response mismatch')
            self.prepare(); self.configure()
        self.release()


def main():
    require(os.geteuid() == 0 and not BASE.is_symlink() and BASE.stat().st_uid == 0
            and BASE.stat().st_mode & 0o077 == 0, 'private root bootstrap required')
    for name in ('profile.json', 'bootstrap.py', 'original-user-data-exit-code'):
        path = BASE / name
        require(not path.is_symlink() and path.stat().st_uid == 0 and path.stat().st_mode & 0o077 == 0, 'untrusted staged file')
    require((BASE / 'original-user-data-exit-code').read_text().strip() == '0', 'original User Data failed')
    profile = json.loads((BASE / 'profile.json').read_text())
    require(profile['helper_sha256'] == sha(Path(__file__).read_bytes()), 'bootstrap checksum mismatch')
    retry(lambda: Bootstrap(profile).start())


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('AKernel image-pull bootstrap pending: ' + type(error).__name__, flush=True)
        raise SystemExit(1)
