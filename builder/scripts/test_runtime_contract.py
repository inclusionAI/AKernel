#!/usr/bin/env python3
"""Contract tests for the RRT-only AKernel runtime package."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[2]


class RuntimeContractTest(unittest.TestCase):
    def assert_artifact_pin(self, dockerfile: str, component: str) -> str:
        prefix = "ADX_RELEASE" if component == "release" else "ADX_EXECD"
        urls = re.findall(rf"^ARG {prefix}_URL=(.+)$", dockerfile, re.MULTILINE)
        checksums = re.findall(
            rf"^ARG {prefix}_SHA256=(.+)$", dockerfile, re.MULTILINE
        )
        self.assertEqual(len(urls), 1, f"missing or duplicate {prefix}_URL pin")
        self.assertEqual(
            len(checksums), 1, f"missing or duplicate {prefix}_SHA256 pin"
        )
        self.assertRegex(
            urls[0],
            rf"^https://openyuanrong\.obs\.cn-southwest-2\.myhuaweicloud\.com/"
            rf"adx/daily/[0-9]{{14}}-[0-9a-f]{{12}}/linux/amd64/"
            rf"adx-{component}\.tar\.gz$",
        )
        self.assertRegex(checksums[0], r"^[0-9a-f]{64}$")
        self.assertIn(
            f'"${{{prefix}_URL}}" -o /tmp/adx-{component}.tar.gz', dockerfile
        )
        self.assertIn(
            f'echo "${{{prefix}_SHA256}}  /tmp/adx-{component}.tar.gz" '
            "| sha256sum -c -;",
            dockerfile,
        )
        return urls[0].rsplit("/", 1)[0]

    def assert_matching_artifact_pins(self, node: str, runtime: str) -> None:
        self.assertEqual(
            self.assert_artifact_pin(node, "release"),
            self.assert_artifact_pin(runtime, "execd"),
            "Node and Execd archives must come from the same ADX build",
        )

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
        self.assert_matching_artifact_pins(node, runtime)
        self.assertIn("install.sh", node)
        self.assertNotIn("adx-release.tar.gz", runtime)
        self.assertNotIn("install.sh", runtime)

        self.assertFalse((ROOT / "builder/adx-release.lock.json").exists())
        self.assertFalse((ROOT / "builder/scripts/fetch_adx_release.py").exists())
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertNotIn("ADX_RELEASE_ARCHIVE", makefile)

    def test_artifact_contract_rejects_unpinned_or_mismatched_builds(self) -> None:
        node = (ROOT / "builder/node.Dockerfile").read_text(encoding="utf-8")
        runtime = (ROOT / "builder/runtime.Dockerfile").read_text(encoding="utf-8")
        mutations = {
            "mutable URL": re.sub(
                r"daily/[0-9]{14}-[0-9a-f]{12}", "daily/latest", node
            ),
            "missing checksum": re.sub(
                r"^ARG ADX_RELEASE_SHA256=.+\n", "", node, flags=re.MULTILINE
            ),
            "invalid checksum": re.sub(
                r"^ARG ADX_RELEASE_SHA256=.+$",
                "ARG ADX_RELEASE_SHA256=invalid",
                node,
                flags=re.MULTILINE,
            ),
            "mismatched build": re.sub(
                r"daily/[0-9]{14}-[0-9a-f]{12}",
                "daily/20000101000000-000000000000",
                node,
            ),
            "unchecked checksum": node.replace("| sha256sum -c -;", "| cat;"),
        }
        for name, mutated_node in mutations.items():
            with self.subTest(name=name), self.assertRaises(AssertionError):
                self.assert_matching_artifact_pins(mutated_node, runtime)

    def test_collector_archive_is_checksum_pinned_before_extraction(self) -> None:
        node = (ROOT / "builder/node.Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(
            node, r"(?m)^ARG OTELCOL_CONTRIB_SHA256=[0-9a-f]{64}$"
        )
        self.assertIn('"${OTELCOL_CONTRIB_URL}" -o /tmp/otelcol-contrib.tar.gz;', node)
        self.assertIn(
            'echo "${OTELCOL_CONTRIB_SHA256}  /tmp/otelcol-contrib.tar.gz" '
            "| sha256sum -c -;",
            node,
        )
        self.assertLess(
            node.index('echo "${OTELCOL_CONTRIB_SHA256}'),
            node.index("tar -xzf /tmp/otelcol-contrib.tar.gz"),
        )

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
