# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Local runtime storage and shared checkpoint mount preflight."""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

CHART = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "checkpoint_prepare", CHART / "scripts/prepare_checkpoint_storage.py"
)
assert SPEC and SPEC.loader
PREPARE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREPARE)

CONFIG = """rootDir="/local/root"
storeDir="/local/store"
[plugin.runtime]
filestore_dir="/local/filestore"
filestore_dir_size="200G"
filestore_xfs_enabled=true
[plugin.runtime.firecracker]
writable_io_engine="AsyncDirect"
writable_cache_type="Writeback"
"""


class PrepareCheckpointStorageTests(unittest.TestCase):
    def test_local_cache_cannot_overlap_private_runtime_state(self):
        for directory in ["/local", "/local/root", "/local/store/cache", "/local/filestore"]:
            with self.subTest(directory=directory), self.assertRaises(ValueError):
                PREPARE.validate_reflink_storage(CONFIG, Path("/shared"), Path(directory))

    def test_reflink_requires_local_materialization_and_native_filestore(self):
        source = CONFIG + '\n[plugin.runtime.runsc]\ncheckpoint_mode="reflink"\n'
        with self.assertRaises(ValueError):
            PREPARE.validate_reflink_storage(source, Path("/shared"), None)
        with self.assertRaises(ValueError):
            PREPARE.validate_reflink_storage(source, Path("/shared"), Path("/local/cache"))

    def test_preparation_preserves_local_runtime_and_does_not_create_shared_runtime(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            mount = root / "shared"
            mount.mkdir()
            source = root / "config.toml"
            source.write_text(CONFIG)
            read_text = Path.read_text

            def fixture_read(path, *args, **kwargs):
                if path == Path("/proc/self/mountinfo"):
                    return f"17 1 0:42 / {mount} rw - fuse.juicefs JuiceFS:akernel rw\n"
                return read_text(path, *args, **kwargs)

            argv = [
                "prepare_checkpoint_storage.py",
                "--config",
                str(source),
                "--mount-root",
                str(mount),
            ]
            with patch("sys.argv", argv), patch.object(Path, "read_text", fixture_read):
                PREPARE.main()
            self.assertEqual(list(mount.iterdir()), [])
            self.assertEqual(source.read_text(), CONFIG)
            self.assertEqual(
                {path.name for path in root.iterdir()}, {"shared", "config.toml"}
            )

    def test_shared_mount_accepts_both_backends_and_rejects_fallback(self):
        root = Path("/mnt/shared")
        for filesystem in ("fuse afs-dfs", "fuse.juicefs JuiceFS:akernel"):
            with self.subTest(filesystem=filesystem):
                PREPARE.shared_mount(
                    root, f"17 1 0:42 / /mnt/shared rw - {filesystem} rw\n"
                )
        for mountinfo in (
            "1 0 8:1 / / rw - xfs /dev/disk rw\n",
            "17 1 8:1 / /mnt/shared rw - xfs /dev/disk rw\n",
            "17 1 0:42 / /mnt/shared ro - fuse afs-dfs rw\n",
            (
                "17 1 0:42 / /mnt/shared rw - fuse afs-dfs rw\n"
                "18 1 8:1 / /mnt/shared rw - xfs /dev/disk rw\n"
            ),
            (
                "17 1 0:42 / /mnt/shared rw - fuse afs-dfs rw\n"
                "18 1 0:42 / /mnt/shared rw - fuse afs-dfs rw\n"
            ),
            (
                "17 1 0:42 / /mnt/shared rw - fuse afs-dfs rw\n"
                "18 1 8:1 / /mnt/shared/runtime rw - xfs /dev/disk rw\n"
            ),
        ):
            with self.subTest(mountinfo=mountinfo), self.assertRaises(ValueError):
                PREPARE.shared_mount(root, mountinfo)

    def test_shared_mount_cannot_host_private_runtime_storage(self):
        for setting in ("rootDir", "storeDir", "filestore_dir"):
            source = CONFIG.replace(
                {
                    "rootDir": "/local/root",
                    "storeDir": "/local/store",
                    "filestore_dir": "/local/filestore",
                }[setting],
                "/shared/private",
            )
            with self.subTest(setting=setting), self.assertRaises(ValueError):
                PREPARE.validate_runtime_storage(source, Path("/shared"))
        with self.assertRaises(ValueError):
            PREPARE.validate_runtime_storage(
                CONFIG + 'storage_dir="/shared/private"\n', Path("/shared")
            )

    def test_local_runtime_policy_and_commented_toml_are_accepted(self):
        source = CONFIG.replace(
            'rootDir="/local/root"', 'rootDir="/local/root" # local runtime'
        )
        PREPARE.validate_runtime_storage(source, Path("/shared"))
        PREPARE.validate_runtime_storage(
            source.split("[plugin.runtime.firecracker]")[0], Path("/shared")
        )
        for storage in ("", "/local/firecracker"):
            PREPARE.validate_runtime_storage(
                source + f'storage_dir="{storage}"\n', Path("/shared")
            )

    def test_runtime_symlink_into_shared_storage_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            shared = root / "shared"
            shared.mkdir()
            link = root / "local"
            link.symlink_to(shared, target_is_directory=True)
            with self.assertRaises(ValueError):
                PREPARE.validate_runtime_storage(
                    CONFIG.replace("/local/", f"{link}/"), shared
                )
            with self.assertRaises(ValueError):
                PREPARE.validate_runtime_storage(
                    CONFIG + f'storage_dir="{link}/firecracker"\n', shared
                )

    def test_runtime_paths_must_be_explicit_absolute_and_without_traversal(self):
        for value in ("relative/root", "/local/../shared/root"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                PREPARE.validate_runtime_storage(
                    CONFIG.replace("/local/root", value), Path("/shared")
                )
            with self.subTest(storage_dir=value), self.assertRaises(ValueError):
                PREPARE.validate_runtime_storage(
                    CONFIG + f'storage_dir="{value}"\n', Path("/shared")
                )
        with self.assertRaises(TypeError):
            PREPARE.validate_runtime_storage(
                CONFIG.replace('rootDir="/local/root"\n', ""), Path("/shared")
            )


if __name__ == "__main__":
    unittest.main()
