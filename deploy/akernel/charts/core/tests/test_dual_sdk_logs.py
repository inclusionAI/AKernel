"""Deployment contracts for one shared native/E2B pool and durable application logs."""
import json
import copy
import hashlib
from pathlib import Path
import subprocess
import unittest

import yaml

CHART = Path(__file__).resolve().parents[1]
E2B = {
    'adx': {'tls': {'existingSecret': 'existing-tls'}, 'e2b': {
        'enabled': True, 'domain': 'sandbox.example.com', 'envdVersion': '0.9.0',
        'envdSha256': 'a' * 64, 'envdIngressSources': ['10.88.0.1']}},
    'node': {'applicationLogs': {'enabled': True}, 'hostDiskPath': '/home/akernel/runtime-v5'},
}


def render(values):
    result = subprocess.run(['helm', 'template', 'akernel', str(CHART), '-n', 'akernel',
                             '-f', '-'], input=yaml.safe_dump(values), text=True,
                            capture_output=True)
    if result.returncode:
        raise ValueError(result.stderr)
    return {(x['kind'], x['metadata']['name']): x for x in yaml.safe_load_all(result.stdout) if x}


class DualSDKLogsTests(unittest.TestCase):
    def test_shared_control_and_worker_pool_and_matching_profiles(self):
        objects = render(E2B)
        self.assertEqual(sum(k[0] == 'Deployment' for k in objects), 2)
        self.assertEqual(sum(k[0] == 'DaemonSet' for k in objects), 1)
        configs = objects['ConfigMap', 'akernel-adx-config']['data']
        ingress, node = (yaml.safe_load(configs[n]) for n in ['ingress-api.yaml', 'node.yaml'])
        self.assertEqual(ingress['runtime_profile'], node['runtime_profile'])
        self.assertEqual(ingress['runtime_profile']['env']['ADX_ENVD_SHA256'], 'a' * 64)
        services = {x['id']: x for x in ingress['services']}
        self.assertEqual(services['apiserver']['config']['e2b']['envdIngressSources'], ['10.88.0.1'])
        routes = services['ingress']['env']['ADX_DATA_PLANE_INGRESS_CONTROL_PLANE_ROUTES']
        for route in ['prefix:/api/environment', 'prefix:/v2/sandboxes', 'prefix:/sandboxes',
                      'prefix:/templates', 'prefix:/v3/templates', 'prefix:/api-keys']:
            self.assertIn(route, routes)

    def test_http_is_opt_in_and_only_changes_ingress_role(self):
        baseline = render(E2B)
        values = copy.deepcopy(E2B)
        values['adx']['e2b']['allowHttp'] = True
        enabled = render(values)
        key = 'ADX_DATA_PLANE_INGRESS_E2B_ALLOW_HTTP'
        before = baseline['ConfigMap', 'akernel-adx-config']['data']
        after = enabled['ConfigMap', 'akernel-adx-config']['data']
        self.assertNotIn(key, before['ingress-api.yaml'])
        env = yaml.safe_load(after['ingress-api.yaml'])['services'][1]['env']
        self.assertEqual(env[key], 'true')
        for role, kind, name in [('node.yaml', 'DaemonSet', 'akernel-node'),
                                  ('coordinator.yaml', 'Deployment', 'akernel-adx-coordinator')]:
            self.assertEqual(before[role], after[role])
            self.assertEqual(baseline[kind, name]['spec']['template'],
                             enabled[kind, name]['spec']['template'])
        self.assertEqual(yaml.safe_load(before['ingress-api.yaml'])['runtime_profile'],
                         yaml.safe_load(after['ingress-api.yaml'])['runtime_profile'])
        self.assertNotEqual(baseline['Deployment', 'akernel-adx-ingress-api']['spec']['template'],
                            enabled['Deployment', 'akernel-adx-ingress-api']['spec']['template'])

    def test_explicit_legacy_pin_fails_closed_when_role_configuration_changes(self):
        values = copy.deepcopy(E2B)
        config = render(values)['ConfigMap', 'akernel-adx-config']['data']['node.yaml']
        values['adx']['configChecksumMigration'] = {'node': {
            'configSha256': hashlib.sha256(config.encode()).hexdigest(),
            'legacyChecksum': 'b' * 64}}
        node = render(values)['DaemonSet', 'akernel-node']
        self.assertEqual(node['spec']['template']['metadata']['annotations']['checksum/adx-config'], 'b' * 64)
        values['adx']['node'] = {'reportIntervalSeconds': 10}
        with self.assertRaises(ValueError):render(values)
        del values['adx']['configChecksumMigration']
        self.assertNotEqual(render(values)['DaemonSet', 'akernel-node']['spec']['template']['metadata']['annotations']['checksum/adx-config'], 'b' * 64)

    def test_http_requires_e2b_and_boolean_configuration(self):
        for value in [True, 'false', 'true']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                render({'adx': {'e2b': {'allowHttp': value}}})

    def test_ingress_canary_readiness_hold_does_not_change_other_roles(self):
        values = copy.deepcopy(E2B)
        values['adx']['ingressApi'] = {'minReadySeconds': 3600, 'progressDeadlineSeconds': 3660, 'replicas': 3}
        objects = render(values)
        ingress = objects['Deployment', 'akernel-adx-ingress-api']['spec']
        self.assertEqual(ingress['minReadySeconds'], 3600)
        self.assertEqual(ingress['progressDeadlineSeconds'], 3660)
        self.assertEqual(ingress['strategy']['rollingUpdate'], {'maxSurge': 0, 'maxUnavailable': 1})
        self.assertNotIn('minReadySeconds', objects['Deployment', 'akernel-adx-coordinator']['spec'])

    def test_canary_requires_a_longer_progress_deadline(self):
        with self.assertRaises(ValueError):
            render({'adx': {'ingressApi': {'minReadySeconds': 3600}}})

    def test_separate_ingress_image_keeps_worker_and_coordinator_pins(self):
        values = copy.deepcopy(E2B)
        values['image'] = {'repository': 'base.example/runtime', 'digest': 'sha256:' + '1' * 64}
        values['coordinator'] = {'image': {'repository': 'base.example/control', 'digest': 'sha256:' + '2' * 64}}
        values['adx']['ingressApi'] = {'image': {'repository': 'base.example/ingress', 'digest': 'sha256:' + '3' * 64}}
        objects = render(values)
        for kind, name, repository, digest in [
                ('DaemonSet', 'akernel-node', 'runtime', '1'),
                ('Deployment', 'akernel-adx-coordinator', 'control', '2'),
                ('Deployment', 'akernel-adx-ingress-api', 'ingress', '3')]:
            pod = objects[kind, name]['spec']['template']['spec']
            for container in pod['containers']:
                self.assertEqual(container['image'], 'base.example/' + repository + '@sha256:' + digest * 64)

    def test_log_mount_is_independent_and_all_initializers_have_guarded_inputs(self):
        objects = render(E2B)
        pod = objects['DaemonSet', 'akernel-node']['spec']['template']['spec']
        logs = next(x for x in pod['volumes'] if x['name'] == 'application-logs-disk')
        self.assertEqual(logs['hostPath'], {'path': '/home/akernel', 'type': 'Directory'})
        prepare = next(x for x in pod['initContainers'] if x['name'] == 'prepare-application-logs')
        self.assertIn('application-logs-host-root', {x['name'] for x in prepare['volumeMounts']})
        main = next(x for x in pod['containers'] if x['name'] == 'akernel-node')
        mount = next(x for x in main['volumeMounts'] if x['mountPath'] == '/app/logs')
        self.assertEqual(mount['name'], 'application-logs-disk')
        self.assertEqual(mount['subPath'], 'shared/app-logs')
        oci = json.loads(objects['ConfigMap', 'akernel-node-config']['data']['config.json'])
        self.assertEqual([x for x in oci['mounts'] if x['destination'] == '/app/logs'][0]['source'], '/app/logs')
        self.assertNotIn('registry_auths.json', str(prepare))

    def test_untrusted_relay_or_missing_artifacts_are_rejected(self):
        for source in ['0.0.0.0', '10.88.0.0/16', '*', '224.0.0.1', 'hostname']:
            values = json.loads(json.dumps(E2B));values['adx']['e2b']['envdIngressSources'] = [source]
            with self.subTest(source=source), self.assertRaises(ValueError):render(values)
        values = json.loads(json.dumps(E2B));values['adx']['e2b']['envdSha256'] = ''
        with self.assertRaises(ValueError):render(values)

    def test_rollback_can_preserve_log_data_without_changing_old_guest_mount_layout(self):
        values = {'adx': {'tls': {'existingSecret': 'tls'}}, 'node': {
            'applicationLogs': {'enabled': True, 'mountInSandbox': False}}}
        objects = render(values)
        oci = json.loads(objects['ConfigMap', 'akernel-node-config']['data']['config.json'])
        self.assertFalse(any(x['destination'] == '/app/logs' for x in oci.get('mounts', [])))
        main = objects['DaemonSet', 'akernel-node']['spec']['template']['spec']['containers'][0]
        self.assertIn('/app/logs', {x['mountPath'] for x in main['volumeMounts']})

    def test_live_registry_credentials_are_preserved_by_default(self):
        values = yaml.safe_load((CHART/'values.yaml').read_text())
        self.assertTrue(values['node']['secret']['preserveExisting'])
        template = (CHART/'templates/node/secrets.yaml').read_text()
        self.assertIn('lookup "v1" "Secret" .Release.Namespace "akernel-secrets"', template)
        self.assertIn('$existing.data', template)

    def test_default_native_does_not_mount_logs_or_activate_envd(self):
        objects = render({'adx': {'tls': {'existingSecret': 'existing-tls'}}})
        self.assertNotIn('ADX_ENVD_PATH', objects['ConfigMap', 'akernel-adx-config']['data']['node.yaml'])
        pod = objects['DaemonSet', 'akernel-node']['spec']['template']['spec']
        self.assertNotIn('application-logs-disk', {x['name'] for x in pod['volumes']})


if __name__ == '__main__':
    unittest.main()
