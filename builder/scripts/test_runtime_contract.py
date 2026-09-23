#!/usr/bin/env python3
"""Contract tests for the RRT-only AKernel runtime package."""

from __future__ import annotations

import unittest
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[2]


class RuntimeContractTest(unittest.TestCase):
    def test_node_service_inherits_deployment_configuration(self) -> None:
        unit = (ROOT / "builder/systemd_services/adx.service").read_text()
        inherited = {
            name
            for line in unit.splitlines()
            if line.startswith("PassEnvironment=")
            for name in line.split("=", 1)[1].split()
        }
        required = {
            "AKERNEL_ADX_CONFIG",
            "AKERNEL_ADX_MANAGED_CREDENTIALS",
            "AKERNEL_ADX_STATE_DIR",
            "ADX_REDIS_URL",
            "NODE_NAME",
            "INSTANCE_IP",
        }
        self.assertFalse(required - inherited, required - inherited)

    def test_sdk_metadata_has_no_actor_runtime_dependency(self) -> None:
        metadata = tomllib.loads(
            (ROOT / "sdk/python/pyproject.toml").read_text(encoding="utf-8")
        )
        requirements = list(metadata["project"]["dependencies"])
        for values in metadata["project"]["optional-dependencies"].values():
            requirements.extend(values)
        normalized = {item.split("=", 1)[0].lower() for item in requirements}
        self.assertNotIn("openyuanrong-sdk", normalized)

    def test_actor_backend_sources_are_absent(self) -> None:
        backend = ROOT / "sdk/python/akernel_sdk/_backends"
        self.assertFalse(list(backend.glob("openyuanrong_sdk*.py")))

    def test_image_does_not_install_retired_control_plane(self) -> None:
        dockerfile = (ROOT / "builder/node.Dockerfile").read_text()
        self.assertNotIn("OPEN_YR", dockerfile)
        self.assertNotIn("yuanrong.service", dockerfile)
        self.assertFalse((ROOT / "builder/systemd_services/yuanrong.service").exists())

    def test_default_sdk_config_keeps_the_existing_address_contract(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        self.assertNotIn("export AKERNEL_GATEWAY_ADDRESS=", workflow)

    def test_actor_runtime_entrypoint_is_absent(self) -> None:
        self.assertFalse((ROOT / "builder/scripts/entryfile.sh").exists())

    def test_image_build_exposes_only_the_execd_profile(self) -> None:
        runtime = (ROOT / "builder/runtime.Dockerfile").read_text(encoding="utf-8")
        build = (ROOT / "deploy/scripts/build-image.sh").read_text(encoding="utf-8")
        self.assertNotIn("openyuanrong_sdk", runtime)
        self.assertNotIn("runtime-python", runtime)
        self.assertIn("--target runtime-execd", build)
        self.assertNotIn("--runtime-profile", build)

    def test_dockerfiles_install_the_pinned_obs_release(self) -> None:
        node = (ROOT / "builder/node.Dockerfile").read_text(encoding="utf-8")
        runtime = (ROOT / "builder/runtime.Dockerfile").read_text(encoding="utf-8")
        release_url = (
            "https://openyuanrong.obs.cn-southwest-2.myhuaweicloud.com/adx/"
            "daily/20260923062527-e295f895e279/linux/amd64/adx-release.tar.gz"
        )
        release_sha256 = (
            "602a738c4367923e8b31a7b0125d5b2069389d7fef93796c86a8d7b758a40dab"
        )
        execd_url = (
            "https://openyuanrong.obs.cn-southwest-2.myhuaweicloud.com/adx/"
            "daily/20260923062527-e295f895e279/linux/amd64/adx-execd.tar.gz"
        )
        execd_sha256 = (
            "8f78c39c568c6c4a61bb5f203f718d7b3f96a5fcbcdad68ffeba3afd394165db"
        )
        self.assertIn(release_url, node)
        self.assertIn(release_sha256, node)
        self.assertIn("install.sh", node)
        self.assertIn("sha256sum -c", node)

        self.assertIn(execd_url, runtime)
        self.assertIn(execd_sha256, runtime)
        self.assertIn("sha256sum -c", runtime)
        self.assertNotIn("adx-release.tar.gz", runtime)
        self.assertNotIn("install.sh", runtime)

        self.assertFalse((ROOT / "builder/adx-release.lock.json").exists())
        self.assertFalse((ROOT / "builder/scripts/fetch_adx_release.py").exists())
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertNotIn("ADX_RELEASE_ARCHIVE", makefile)

    def test_node_image_copies_from_the_verified_install_tree(self) -> None:
        node = (ROOT / "builder/node.Dockerfile").read_text(encoding="utf-8")
        self.assertNotIn("/adx-package", node)
        self.assertIn(
            "COPY --from=adx-release /opt/adx/current/bin/ "
            "/opt/adx/current/bin/",
            node,
        )
        self.assertIn(
            "COPY --from=adx-release /opt/adx/current/runtime/adx-execd ",
            node,
        )

    def test_runtime_rootfs_has_only_the_adx_layout(self) -> None:
        runtime = (ROOT / "builder/runtime.Dockerfile").read_text(encoding="utf-8")
        self.assertNotIn("/adx-package", runtime)
        self.assertNotIn("/var/task/code", runtime)
        self.assertNotIn("/__yuanrong", runtime)
        self.assertIn("mkdir -p /var/task /__adx", runtime)
        self.assertIn(
            "COPY --from=adx-execd /opt/adx-execd/adx-execd ",
            runtime,
        )

    def test_build_targets_the_release_architecture(self) -> None:
        build = (ROOT / "deploy/scripts/build-image.sh").read_text(encoding="utf-8")
        self.assertIn('AKERNEL_TARGET_PLATFORM:-linux/amd64', build)
        self.assertIn('--platform "${target_platform}"', build)

    def test_image_uses_current_adx_component_names(self) -> None:
        node = (ROOT / "builder/node.Dockerfile").read_text(encoding="utf-8")
        runtime = (ROOT / "builder/runtime.Dockerfile").read_text(encoding="utf-8")
        for component in ("adx-coordinator", "adxlet", "adx-apiserver"):
            self.assertIn(component, node)
        self.assertIn("adx-execd", runtime)
        for retired in ("adx-master", "adx-node-manager", "adx-api-server"):
            self.assertNotIn(retired, node)
        self.assertNotIn("rrt-runtime", runtime)


if __name__ == "__main__":
    unittest.main()
