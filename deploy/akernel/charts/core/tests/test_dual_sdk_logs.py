"""Deployment contracts for one shared native/E2B pool and durable application logs."""
import json
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
