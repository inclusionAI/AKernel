#!/usr/bin/env python3
"""Directory lifecycle and rendered operator-init contract tests (no cluster)."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml


CHART = Path(__file__).resolve().parents[1]
SCRIPT = CHART / "scripts/prepare-node-directories.py"
EXAMPLE = CHART.parents[1] / "examples/node-local-directories.yaml"
SPEC = importlib.util.spec_from_file_location("prepare_node_directories", SCRIPT)
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


class DirectoryPreparationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.item = {"path": "shared/logs", "uid": os.geteuid(), "gid": os.getegid(), "mode": "0770"}
        self.mount = patch.object(helper, "is_volume_root", side_effect=lambda root: root == str(self.root))
        self.mount.start()
        self.addCleanup(self.mount.stop)

    def prepare(self, *items):
        return helper.prepare(str(self.root), helper.declarations(json.dumps(items or [self.item])))

    def test_create_and_repeat_preserves_data_inode_and_permissions(self):
        self.assertEqual(self.prepare()[0]["action"], "created")
        directory = self.root / "shared/logs"
        data = directory / "existing.log"
        data.write_text("operator data\n")
        data.chmod(0o600)
        before = directory.stat()
        self.assertEqual(self.prepare()[0]["action"], "exists")
        self.assertEqual(directory.stat().st_ino, before.st_ino)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o770)
        self.assertEqual(data.read_text(), "operator data\n")
        self.assertEqual(stat.S_IMODE(data.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(directory.parent.stat().st_mode), 0o755)

    def test_setgid_mode_is_preserved(self):
        self.prepare(dict(self.item, mode="2770"))
        self.assertEqual(stat.S_IMODE((self.root / "shared/logs").stat().st_mode), 0o2770)

    def test_example_capabilities_preserve_setgid_for_application_group(self):
        if os.geteuid() != 0 or not shutil.which("setpriv"):
            self.skipTest("requires Linux root and setpriv to restrict capabilities")
        initializer = yaml.safe_load(EXAMPLE.read_text())["core"]["node"]["initContainers"][0]
        caps = initializer["securityContext"]["capabilities"]["add"]
        numbers = {"CHOWN": 0, "DAC_OVERRIDE": 1, "FOWNER": 3, "FSETID": 4}
        bounding = next(line.split()[1] for line in Path("/proc/self/status").read_text().splitlines()
                        if line.startswith("CapBnd:"))
        if any(not int(bounding, 16) & (1 << numbers[cap]) for cap in caps):
            self.skipTest("parent capability bounding set cannot grant the example's capabilities")
        program = """import importlib.util,sys
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('directories',sys.argv[1])
helper=importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
with patch.object(helper,'is_volume_root',return_value=True):
    raise SystemExit(helper.main(sys.argv[2:]))
"""
        target_gid = 1000
        while target_gid in {*os.getgroups(), os.getegid()}:
            target_gid += 1
        item = dict(self.item, uid=1000, gid=target_gid, mode="2770")
        result = subprocess.run(
            ["setpriv", "--no-new-privs", "--bounding-set=-all," + ",".join("+" + cap.lower() for cap in caps),
             "--", sys.executable, "-c", program, str(SCRIPT), "--root", str(self.root),
             "--directories-json", json.dumps([item])], capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        info = (self.root / item["path"]).stat()
        self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)), (1000, target_gid, 0o2770))

    def test_permission_mismatch_does_not_repair_or_delete_existing_data(self):
        self.prepare()
        directory = self.root / "shared/logs"
        data = directory / "existing.log"
        data.write_text("retain me")
        directory.chmod(0o750)
        with self.assertRaisesRegex(ValueError, "permissions mismatch.*mode=0770.*mode=0750"):
            self.prepare()
        self.assertEqual(data.read_text(), "retain me")
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o750)

    def test_owner_mismatch_does_not_chown_existing_directory(self):
        self.prepare()
        with self.assertRaisesRegex(ValueError, "permissions mismatch"):
            self.prepare(dict(self.item, uid=os.geteuid() + 1))
        self.assertEqual((self.root / "shared/logs").stat().st_uid, os.geteuid())

    def test_parent_and_leaf_symlinks_are_never_followed(self):
        target = self.root / "outside"
        target.mkdir()
        (self.root / "shared").symlink_to(target, target_is_directory=True)
        with self.assertRaises(OSError):
            self.prepare()
        self.assertEqual(list(target.iterdir()), [])
        (self.root / "shared").unlink()
        (self.root / "shared").mkdir()
        (self.root / "shared/logs").symlink_to(target, target_is_directory=True)
        with self.assertRaises(OSError):
            self.prepare()
        self.assertEqual(list(target.iterdir()), [])

    def test_missing_symlink_target_is_not_created(self):
        (self.root / "shared").symlink_to(self.root / "absent", target_is_directory=True)
        with self.assertRaises(OSError):
            self.prepare()
        self.assertFalse((self.root / "absent").exists())

    def test_existing_file_is_not_replaced(self):
        (self.root / "shared").write_text("keep")
        with self.assertRaises(OSError):
            self.prepare()
        self.assertEqual((self.root / "shared").read_text(), "keep")

    def test_writable_parent_and_root_are_rejected(self):
        (self.root / "shared").mkdir(mode=0o777)
        (self.root / "shared").chmod(0o777)
        with self.assertRaisesRegex(ValueError, "unsafe parent"):
            self.prepare()
        self.assertFalse((self.root / "shared/logs").exists())
        self.root.chmod(0o777)
        with self.assertRaisesRegex(ValueError, "unsafe parent"):
            self.prepare()

    def test_unmounted_root_fails_before_writing(self):
        with patch.object(helper, "is_volume_root", return_value=False):
            with self.assertRaisesRegex(ValueError, "volume root is not mounted"):
                self.prepare()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_root_symlink_and_traversal_are_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        for root in (str(alias), str(alias / "child"), str(self.root) + "/../elsewhere", "/", "relative"):
            with self.subTest(root=root), self.assertRaises(ValueError):
                helper.prepare(root, helper.declarations(json.dumps([self.item])))

    def test_invalid_declarations_are_rejected_before_any_creation(self):
        bad = [
            dict(self.item, path=p) for p in ("", "/", "/absolute", "../outside", "x/../y", "x//y", "x/", ".", "x/./y", "x\n")
        ] + [dict(self.item, uid=v) for v in (True, -1, 2**32 - 1, "1000")]
        bad += [dict(self.item, gid=False), dict(self.item, mode=770), dict(self.item, mode="8888"),
                dict(self.item, mode="770"), dict(self.item, unknown=1)]
        for item in bad:
            with self.subTest(item=item), self.assertRaises(ValueError):
                helper.declarations(json.dumps([dict(self.item, path="valid"), item]))
        for text in ('{}', '[null]', '[{"path":"x","path":"y"}]', '[',
                     json.dumps([self.item, self.item]),
                     json.dumps([self.item, dict(self.item, path="shared")])):
            with self.subTest(text=text), self.assertRaises(ValueError):
                helper.declarations(text)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_mountinfo_matches_exact_mountpoint_and_decodes_spaces(self):
        self.mount.stop()
        content = "1 0 8:1 / /data\\040disk rw - ext4 /dev/test rw\n"
        with patch.object(Path, "open", return_value=io.StringIO(content)):
            self.assertTrue(helper.is_volume_root("/data disk"))
        with patch.object(Path, "open", return_value=io.StringIO(content)):
            self.assertFalse(helper.is_volume_root("/data disk/child"))

    def test_cli_reports_failure_and_success_without_traceback(self):
        args = ["--root", str(self.root), "--directories-json", json.dumps([self.item])]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(helper.main(args), 0)
        self.assertEqual(json.loads(output.getvalue())["directories"][0]["action"], "created")
        with contextlib.redirect_stderr(output):
            self.assertEqual(helper.main(["--root", str(self.root), "--directories-json", "invalid"]), 1)
        self.assertIn("node directory preparation failed", output.getvalue())


class DirectoryChartTest(unittest.TestCase):
    def render(self, chart, values):
        result = subprocess.run(["helm", "template", "test", str(chart), "--namespace", "test", "-f", "-"],
                                input=yaml.safe_dump(values), check=True, capture_output=True, text=True)
        resources = [r for r in yaml.safe_load_all(result.stdout) if r]
        ds = next(r for r in resources if r["kind"] == "DaemonSet")
        config = next(r for r in resources if r["kind"] == "ConfigMap" and r["metadata"]["name"] == "akernel-node-config")
        return ds["spec"]["template"], config["data"]

    def test_default_does_not_prepare_operator_directories(self):
        template, config = self.render(CHART, {})
        self.assertEqual([c["name"] for c in template["spec"]["initContainers"]], ["migrate-adx-state"])
        self.assertNotIn("prepare-node-directories.py", json.dumps(template))
        self.assertNotIn("prepare-node-directories.py", config)

    def test_unused_helper_changes_preserve_pod_template_and_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            changed = Path(directory) / "core"
            shutil.copytree(CHART, changed)
            (changed / SCRIPT.relative_to(CHART)).write_text(SCRIPT.read_text() + "\n# helper update\n")
            existing_init = {"name": "existing-preparation", "image": "busybox:1.37", "command": ["true"]}
            for initializers in ([], [existing_init]):
                with self.subTest(initializers=initializers):
                    values = {"node": {"initContainers": initializers}}
                    self.assertEqual(self.render(CHART, values), self.render(changed, values))

    def test_old_release_values_without_helper_setting_render_unchanged(self):
        # --reuse-values can retain the old chart defaults without new keys.
        with tempfile.TemporaryDirectory() as directory:
            legacy = Path(directory) / "core"
            shutil.copytree(CHART, legacy)
            defaults = yaml.safe_load((legacy / "values.yaml").read_text())
            del defaults["node"]["directoryHelper"]
            (legacy / "values.yaml").write_text(yaml.safe_dump(defaults))
            self.assertEqual(self.render(CHART, {}), self.render(legacy, {}))

    def test_opted_in_helper_changes_update_node_checksum(self):
        values = yaml.safe_load(EXAMPLE.read_text())["core"]
        before, _ = self.render(CHART, values)
        with tempfile.TemporaryDirectory() as directory:
            changed = Path(directory) / "core"
            shutil.copytree(CHART, changed)
            (changed / SCRIPT.relative_to(CHART)).write_text(SCRIPT.read_text() + "\n# helper update\n")
            after, _ = self.render(changed, values)
        self.assertEqual(before["spec"], after["spec"])
        self.assertNotEqual(before["metadata"]["annotations"]["checksum/node-config"],
                            after["metadata"]["annotations"]["checksum/node-config"])

    def test_supported_example_and_packaged_helper_with_each_volume_type(self):
        example = yaml.safe_load(EXAMPLE.read_text())
        for umbrella in (False, True):
            for ephemeral in (False, True):
                with self.subTest(umbrella=umbrella, ephemeral=ephemeral):
                    core = dict(example["core"])
                    core["node"] = dict(core["node"], homeDisk={"useEphemeralPVC": ephemeral})
                    values = {"core": core, "monitor": {"enabled": False}} if umbrella else core
                    template, config = self.render(CHART.parents[1] if umbrella else CHART, values)
                    pod = template["spec"]
                    self.assertEqual(config["prepare-node-directories.py"], SCRIPT.read_text())
                    self.assertEqual([c["name"] for c in pod["initContainers"]],
                                     ["migrate-adx-state", "prepare-node-directories"])
                    init = pod["initContainers"][1]
                    self.assertEqual(init, example["core"]["node"]["initContainers"][0])
                    spec = helper.declarations(init["args"][3])
                    self.assertEqual(spec[0]["path"], "shared/app-logs")
                    volume = next(v for v in pod["volumes"] if v["name"] == "home-disk")
                    self.assertIn("ephemeral" if ephemeral else "hostPath", volume)


if __name__ == "__main__":
    unittest.main()
