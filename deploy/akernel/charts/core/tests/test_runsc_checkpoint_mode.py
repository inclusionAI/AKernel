"""Render the explicit runsc policy; reject unsupported silent fallbacks."""
import subprocess
import unittest
from pathlib import Path

import yaml

CHART = Path(__file__).resolve().parents[1]


def render(*overrides):
    return subprocess.run(
        ['helm', 'template', 'akernel', str(CHART), *overrides],
        capture_output=True, text=True,
    )


def config(result):
    assert result.returncode == 0, result.stderr
    return next(row['data']['sandboxd_config.toml']
                for row in yaml.safe_load_all(result.stdout)
                if row and row['kind'] == 'ConfigMap' and row['metadata']['name'] == 'akernel-node-config')


class RunscCheckpointModeTest(unittest.TestCase):
    def test_reflink_profile_sets_only_runsc_mode(self):
        text = config(render('--set', 'node.config.sandboxd.runscCheckpointMode=reflink'))
        section = text.split('[plugin.runtime.runsc]', 1)[1].split('\n[', 1)[0]
        self.assertIn('checkpoint_mode="reflink"', section)
        self.assertIn('background_restore=true', section)
        self.assertEqual(text.count('checkpoint_mode="reflink"'), 1)

    def test_default_leaves_runtime_default_unchanged(self):
        text = config(render())
        section = text.split('[plugin.runtime.runsc]', 1)[1].split('\n[', 1)[0]
        self.assertNotIn('checkpoint_mode=', section)

    def test_memory_incremental_is_not_accepted_as_runsc_mode(self):
        result = render('--set', 'node.config.sandboxd.runscCheckpointMode=incremental')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('runscCheckpointMode', result.stderr)

    def test_custom_config_must_have_explicit_injection_marker(self):
        result = render('--set', 'node.config.sandboxd.runscCheckpointMode=reflink',
                        '--set-string', 'node.config.sandboxd.config=[plugin.runtime.runsc]')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('AKERNEL_RUNSC_CHECKPOINT_MODE', result.stderr)


if __name__ == '__main__':
    unittest.main()
