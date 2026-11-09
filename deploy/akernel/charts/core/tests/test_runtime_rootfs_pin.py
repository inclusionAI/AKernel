"""The runtime rootfs must remain immutable across control-plane upgrades."""
import subprocess
import unittest
from pathlib import Path

import yaml

CHART = Path(__file__).resolve().parents[1]
SOURCE = 'example.test/platform@sha256:' + 'a' * 64


def render(**changes):
    values = {'node.runtimeRootfsPin.enabled': 'true',
              'node.runtimeRootfsPin.sourceImage': SOURCE,
              'node.runtimeRootfsPin.sha256': 'b' * 64,
              'node.runtimeRootfsPin.execdSha256': 'c' * 64}
    values.update(changes)
    command = ['helm', 'template', 'test', str(CHART), '--set',
               'adx.tls.existingSecret=test-tls']
    for key, value in values.items():
        command.extend(['--set' if key.endswith('.enabled') else '--set-string', key + '=' + value])
    return subprocess.run(command, text=True, capture_output=True, check=False)


class RuntimeRootfsPinTest(unittest.TestCase):
    def test_pin_stages_old_artifact_but_verifies_current_execd(self):
        result = render()
        self.assertEqual(result.returncode, 0, result.stderr)
        objects = [item for item in yaml.safe_load_all(result.stdout) if item]
        node = next(item for item in objects if item['kind'] == 'DaemonSet')
        spec = node['spec']['template']['spec']
        initializers = {item['name']: item for item in spec['initContainers']}
        self.assertIn('stage-runtime-rootfs', initializers)
        self.assertEqual(initializers['stage-runtime-rootfs']['image'], SOURCE)
        main = next(item for item in spec['containers'] if item['name'] == 'akernel-node')
        self.assertEqual(initializers['verify-runtime-rootfs']['image'], main['image'])
        mount = next(item for item in main['volumeMounts'] if
                     item['mountPath'] == '/opt/akernel/runtime/akernel-runtime-rootfs.img')
        self.assertTrue(mount['readOnly'])
        self.assertEqual(mount['name'], 'home-disk')
        self.assertEqual(mount['subPath'], 'runtime-rootfs/' + 'b' * 64 + '.img')
        self.assertIn('c' * 64, initializers['verify-runtime-rootfs']['args'])
        config = next(item for item in objects if item['kind'] == 'ConfigMap' and
                      item['metadata']['name'] == 'akernel-node-config')
        self.assertIn('pin-runtime-rootfs.py', config['data'])

    def test_rejects_unpinned_source_and_invalid_hashes(self):
        for changes in ({'node.runtimeRootfsPin.sourceImage': 'example.test/platform:latest'},
                        {'node.runtimeRootfsPin.sha256': '../image'},
                        {'node.runtimeRootfsPin.execdSha256': ''}):
            with self.subTest(changes=changes):
                self.assertNotEqual(render(**changes).returncode, 0)

    def test_separately_qualified_current_execd_does_not_change_source_or_guest(self):
        result = render(**{'node.runtimeRootfsPin.currentExecdSha256': 'd' * 64})
        self.assertEqual(result.returncode, 0, result.stderr)
        objects = [item for item in yaml.safe_load_all(result.stdout) if item]
        spec = next(item for item in objects if item['kind'] == 'DaemonSet')['spec']['template']['spec']
        initializers = {item['name']: item for item in spec['initContainers']}
        self.assertIn('c' * 64, initializers['stage-runtime-rootfs']['args'])
        self.assertNotIn('d' * 64, initializers['stage-runtime-rootfs']['args'])
        self.assertIn('d' * 64, initializers['verify-runtime-rootfs']['args'])
        self.assertNotIn('c' * 64, initializers['verify-runtime-rootfs']['args'])
        main = next(item for item in spec['containers'] if item['name'] == 'akernel-node')
        mount = next(item for item in main['volumeMounts'] if item['mountPath'] == '/opt/akernel/runtime/akernel-runtime-rootfs.img')
        self.assertEqual(mount['subPath'], 'runtime-rootfs/' + 'b' * 64 + '.img')
        self.assertTrue(mount['readOnly'])
        for invalid in ['../image', 'd' * 63, 'D' * 64]:
            with self.subTest(invalid=invalid):
                self.assertNotEqual(render(**{'node.runtimeRootfsPin.currentExecdSha256': invalid}).returncode, 0)

    def test_disabled_pin_leaves_worker_runtime_mounts_unchanged(self):
        result = render(**{'node.runtimeRootfsPin.enabled': 'false'})
        self.assertEqual(result.returncode, 0, result.stderr)
        objects = [item for item in yaml.safe_load_all(result.stdout) if item]
        spec = next(item for item in objects if item['kind'] == 'DaemonSet')['spec']['template']['spec']
        self.assertFalse(any(item['name'].endswith('-runtime-rootfs') for item in spec['initContainers']))
        main = next(item for item in spec['containers'] if item['name'] == 'akernel-node')
        self.assertFalse(any(item['mountPath'] == '/opt/akernel/runtime/akernel-runtime-rootfs.img'
                             for item in main['volumeMounts']))

    def test_envd_profile_requires_separate_qualification(self):
        self.assertNotEqual(render(**{'adx.e2b.enabled': 'true'}).returncode, 0)


if __name__ == '__main__':
    unittest.main()
