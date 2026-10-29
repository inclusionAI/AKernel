# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Shared checkpoint deployment contracts, without a Kubernetes cluster."""

from __future__ import annotations

import subprocess
import tomllib
import unittest
from pathlib import Path

import yaml

CHART = Path(__file__).resolve().parents[1]


def render(*settings: str) -> dict[tuple[str, str], dict]:
    command = ["helm", "template", "akernel", str(CHART), "--namespace", "akernel-test"]
    for setting in settings:
        command.extend(["--set-string", setting])
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    return {
        (item["kind"], item["metadata"]["name"]): item
        for item in yaml.safe_load_all(result.stdout)
        if item
    }


class SharedCheckpointStorageTests(unittest.TestCase):
    def test_control_service_budgets_protect_voluntary_disruption(self):
        for replicas in (1, 3):
            resources = render(f"adx.ingressApi.replicas={replicas}")
            self.assertEqual(resources[("PodDisruptionBudget", "akernel-adx-coordinator")]["spec"]["minAvailable"], 1)
            if replicas == 1:
                self.assertNotIn(("PodDisruptionBudget", "akernel-adx-ingress-api"), resources)
            else:
                self.assertEqual(resources[("PodDisruptionBudget", "akernel-adx-ingress-api")]["spec"]["minAvailable"], 2)

    def test_core_images_accept_a_shared_immutable_digest(self):
        digest = "sha256:" + "a" * 64
        resources = render("image.repository=example/akernel", f"image.digest={digest}")
        for key in [("Deployment", "akernel-adx-coordinator"),
                    ("Deployment", "akernel-adx-ingress-api"), ("DaemonSet", "akernel-node")]:
            pod = resources[key]["spec"]["template"]["spec"]
            self.assertEqual(pod["containers"][0]["image"], f"example/akernel@{digest}")
        with self.assertRaises(subprocess.CalledProcessError):
            render("image.digest=latest")

    def test_control_services_use_control_pool_without_losing_mount_placement(self):
        resources = self.shared_resources(
            "adx.checkpointStorage.nodeSelector.storage=juicefs",
            "adx.coordinator.nodeSelector.node-role=akernel-control-plane",
            "adx.coordinator.tolerations[0].key=dedicated",
            "adx.coordinator.tolerations[0].value=akernel-control-plane",
            "adx.coordinator.tolerations[0].effect=NoSchedule",
            "adx.ingressApi.nodeSelector.node-role=akernel-control-plane",
        )
        coord = resources[("Deployment", "akernel-adx-coordinator")]["spec"]["template"]["spec"]
        self.assertEqual(coord["nodeSelector"], {"storage": "juicefs", "node-role": "akernel-control-plane"})
        self.assertEqual(coord["tolerations"][0]["value"], "akernel-control-plane")
        ingress = resources[("Deployment", "akernel-adx-ingress-api")]["spec"]["template"]["spec"]
        self.assertEqual(ingress["nodeSelector"], {"node-role": "akernel-control-plane"})
        with self.assertRaises(subprocess.CalledProcessError):
            self.shared_resources("adx.checkpointStorage.nodeSelector.pool=workers", "adx.coordinator.nodeSelector.pool=control")

    def test_worker_selector_can_target_a_canary_without_selecting_coordinator(self):
        resources = self.shared_resources(
            "adx.checkpointStorage.nodeSelector.storage=juicefs",
            "node.nodeSelector.generation=canary",
        )
        node = resources[("DaemonSet", "akernel-node")]["spec"]["template"]["spec"]
        coordinator = resources[("Deployment", "akernel-adx-coordinator")]["spec"]["template"]["spec"]
        self.assertEqual(node["nodeSelector"], {"storage": "juicefs", "generation": "canary"})
        self.assertEqual(coordinator["nodeSelector"], {"storage": "juicefs"})
        with self.assertRaises(subprocess.CalledProcessError):
            self.shared_resources("adx.checkpointStorage.nodeSelector.pool=clients", "node.nodeSelector.pool=other")

    def test_local_cache_is_node_owned_and_reclaimer_stays_shared_only(self) -> None:
        resources = self.shared_resources(
            "adx.checkpointStorage.localCache.enabled=true",
        )
        configs = resources[("ConfigMap", "akernel-adx-config")]["data"]
        node = yaml.safe_load(configs["node.yaml"])["services"][0]["config"]
        coordinator = yaml.safe_load(configs["coordinator.yaml"])["services"][0]["config"]
        self.assertEqual(node["checkpoint_storage"]["local_cache"], {
            "root": "/home/akernel/adx/checkpoints", "budget_bytes": 53687091200,
            "max_entries": 500,
        })
        self.assertNotIn("local_cache", coordinator["checkpoint_reclaimers"][0])
        pod = resources[("DaemonSet", "akernel-node")]["spec"]["template"]["spec"]
        preflight = next(item for item in pod["initContainers"] if item["name"] == "prepare-checkpoint-storage")
        self.assertIn("--local-cache-root", preflight["args"])
        home = next(item for item in preflight["volumeMounts"] if item["name"] == "home-disk")
        self.assertFalse(home.get("readOnly", False))

    def test_reflink_profile_uses_native_filestore_and_keeps_background_restore(self) -> None:
        resources = self.shared_resources(
            "adx.checkpointStorage.localCache.enabled=true",
            "node.config.sandboxd.nativeFilestore=true",
            "node.config.sandboxd.runscCheckpointMode=reflink",
        )
        config = tomllib.loads(resources[("ConfigMap", "akernel-node-config")]["data"]["sandboxd_config.toml"])
        runtime = config["plugin"]["runtime"]
        self.assertEqual(runtime["filestore_dir_size"], "")
        self.assertEqual(runtime["runsc"]["checkpoint_mode"], "reflink")
        self.assertTrue(runtime["runsc"]["background_restore"])
        self.assertTrue(runtime["checkpoint_sync"]["sync_output"])

    def test_cached_filesystem_rejects_unsafe_roots_and_invalid_limits(self) -> None:
        for setting in (
            "adx.checkpointStorage.localCache.root=/home/akernel/filestore/cache",
            "adx.checkpointStorage.localCache.root=/tmp/checkpoints",
            "adx.checkpointStorage.localCache.budgetBytes=0",
            "adx.checkpointStorage.localCache.maxEntries=0",
            "adx.checkpointStorage.localCache.maxEntries=2.5",
        ):
            with self.subTest(setting=setting), self.assertRaises(subprocess.CalledProcessError):
                self.shared_resources("adx.checkpointStorage.localCache.enabled=true", setting)
        with self.assertRaises(subprocess.CalledProcessError):
            render("adx.checkpointStorage.localCache.enabled=true")
        with self.assertRaises(subprocess.CalledProcessError):
            self.shared_resources("node.config.sandboxd.runscCheckpointMode=reflink")

    def test_shared_consumers_use_mount_client_placement_and_keep_affinity(
        self,
    ) -> None:
        resources = self.shared_resources(
            "adx.checkpointStorage.nodeSelector.storage=juicefs",
            "node.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[0].matchExpressions[0].key=pool",
            "node.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[0].matchExpressions[0].operator=In",
            "node.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[0].matchExpressions[0].values[0]=workers",
        )
        for kind, name in (
            ("Deployment", "akernel-adx-coordinator"),
            ("DaemonSet", "akernel-node"),
        ):
            pod = resources[(kind, name)]["spec"]["template"]["spec"]
            self.assertEqual(pod["nodeSelector"], {"storage": "juicefs"})
        node = resources[("DaemonSet", "akernel-node")]["spec"]["template"]["spec"]
        self.assertEqual(
            node["affinity"]["nodeAffinity"][
                "requiredDuringSchedulingIgnoredDuringExecution"
            ]["nodeSelectorTerms"][0]["matchExpressions"][0]["key"],
            "pool",
        )
        for kind, name in (
            ("Deployment", "akernel-adx-coordinator"),
            ("DaemonSet", "akernel-node"),
        ):
            self.assertNotIn(
                "nodeSelector", render()[(kind, name)]["spec"]["template"]["spec"]
            )

    def test_firecracker_default_preserves_erofs_without_virtiofs(self) -> None:
        config = tomllib.loads(
            render()[("ConfigMap", "akernel-node-config")]["data"][
                "sandboxd_config.toml"
            ]
        )
        self.assertFalse(config["plugin"]["runtime"]["firecracker"]["virtiofs_enabled"])
        standalone = tomllib.loads(
            (CHART.parents[2] / "standalone/config/sandboxd_config.toml").read_text()
        )
        self.assertFalse(
            standalone["plugin"]["runtime"]["firecracker"]["virtiofs_enabled"]
        )

    def test_aliases_follow_filesystem_backend_contract(self) -> None:
        for alias in ("akernel-checkpoints", "Team9", "-cache", "LOCAL"):
            with self.subTest(alias=alias):
                self.shared_resources(f"adx.checkpointStorage.alias={alias}")
        # Local mode does not instantiate the distributed filesystem store.
        render("adx.checkpointStorage.alias=local")

    def test_ingress_rollout_preserves_one_replica_without_a_spare_node(self) -> None:
        for replicas in (1, 2, 3):
            with self.subTest(replicas=replicas):
                spec = render(f"adx.ingressApi.replicas={replicas}")[
                    ("Deployment", "akernel-adx-ingress-api")
                ]["spec"]
                self.assertEqual(
                    spec["strategy"],
                    {
                        "type": "RollingUpdate",
                        "rollingUpdate": {
                            "maxSurge": 0 if replicas > 1 else 1,
                            "maxUnavailable": 1 if replicas > 1 else 0,
                        },
                    },
                )

    def test_runtime_push_is_enabled_with_node_private_credentials(self) -> None:
        for resources in (render(), self.shared_resources()):
            node = yaml.safe_load(
                resources[("ConfigMap", "akernel-adx-config")]["data"]["node.yaml"]
            )["services"][0]["config"]
            self.assertEqual(
                node["runtime_control"],
                {
                    "listen": "0.0.0.0:19003",
                    "advertised_address": "http://${INSTANCE_IP}:19003",
                    "key_file": "/home/akernel/adx/runtime-control-key",
                },
            )

    def test_runtime_push_port_and_required_lifecycle_events(self) -> None:
        resources = render("adx.runtimeControl.port=19023")
        node = yaml.safe_load(
            resources[("ConfigMap", "akernel-adx-config")]["data"]["node.yaml"]
        )["services"][0]["config"]
        self.assertEqual(node["runtime_control"]["listen"], "0.0.0.0:19023")
        self.assertEqual(
            node["runtime_control"]["advertised_address"], "http://${INSTANCE_IP}:19023"
        )
        with self.assertRaises(subprocess.CalledProcessError) as error:
            render("adx.runtimeControl.enabled=false")
        self.assertIn("lifecycle events require runtime control", error.exception.stderr)

    def test_runtime_push_rejects_invalid_ports(self) -> None:
        for port in ("0", "65536", "-1", "invalid", "19003.5"):
            with (
                self.subTest(port=port),
                self.assertRaises(subprocess.CalledProcessError),
            ):
                render(f"adx.runtimeControl.port={port}")

    def shared_resources(self, *settings: str) -> dict[tuple[str, str], dict]:
        return render(
            "adx.checkpointStorage.kind=distributed_fs",
            "adx.checkpointStorage.hostPath=/srv/juicefs",
            *settings,
        )

    def test_local_default_keeps_node_checkpoint_directory(self) -> None:
        resources = render()
        configs = resources[("ConfigMap", "akernel-adx-config")]["data"]
        node = yaml.safe_load(configs["node.yaml"])["services"][0]["config"]
        self.assertEqual(node["checkpoint_dir"], "/home/akernel/adx/checkpoints")
        self.assertNotIn("checkpoint_storage", node)
        coordinator = yaml.safe_load(configs["coordinator.yaml"])["services"][0][
            "config"
        ]
        self.assertNotIn("checkpoint_reclaimers", coordinator)

    def test_nodes_and_coordinator_share_one_storage_contract(self) -> None:
        resources = self.shared_resources()
        configs = resources[("ConfigMap", "akernel-adx-config")]["data"]
        node = yaml.safe_load(configs["node.yaml"])["services"][0]["config"]
        coordinator = yaml.safe_load(configs["coordinator.yaml"])["services"][0][
            "config"
        ]
        storage = {
            "kind": "distributed_fs",
            "alias": "akernel-checkpoints",
            "mount_root": "/mnt/akernel-shared",
            "prefix": "akernel/checkpoints",
        }
        self.assertEqual(node["checkpoint_storage"], storage)
        self.assertNotIn("checkpoint_dir", node)
        self.assertEqual(coordinator["checkpoint_reclaimers"], [storage])
        for kind, name in (
            ("Deployment", "akernel-adx-coordinator"),
            ("DaemonSet", "akernel-node"),
        ):
            with self.subTest(component=name):
                pod = resources[(kind, name)]["spec"]["template"]["spec"]
                volume = next(
                    item
                    for item in pod["volumes"]
                    if item["name"] == "checkpoint-storage"
                )
                self.assertEqual(
                    volume["hostPath"], {"path": "/srv/juicefs", "type": "Directory"}
                )
                mount = next(
                    item
                    for item in pod["containers"][0]["volumeMounts"]
                    if item["name"] == "checkpoint-storage"
                )
                self.assertEqual(mount["mountPath"], storage["mount_root"])
                self.assertEqual(mount["mountPropagation"], "HostToContainer")
        ingress = resources[("Deployment", "akernel-adx-ingress-api")]["spec"][
            "template"
        ]["spec"]
        self.assertFalse(
            any(item["name"] == "checkpoint-storage" for item in ingress["volumes"])
        )

    def test_shared_checkpoint_mount_leaves_private_runtime_on_node_disk(self) -> None:
        resources = self.shared_resources()
        node = resources[("DaemonSet", "akernel-node")]["spec"]["template"]["spec"]
        init = next(
            item
            for item in node["initContainers"]
            if item["name"] == "prepare-checkpoint-storage"
        )
        self.assertEqual(
            init["args"],
            [
                "--config",
                "/config/sandboxd_config.toml",
                "--mount-root",
                "/mnt/akernel-shared",
            ],
        )
        local = next(m for m in init["volumeMounts"] if m["name"] == "home-disk")
        self.assertEqual(
            local, {"name": "home-disk", "mountPath": "/home/akernel", "readOnly": True}
        )
        mounts = node["containers"][0]["volumeMounts"]
        self.assertEqual(
            [m["mountPath"] for m in mounts if m["name"] == "checkpoint-storage"],
            ["/mnt/akernel-shared"],
        )
        home = next(m for m in mounts if m["mountPath"] == "/home/akernel")
        self.assertEqual(home["name"], "home-disk")
        config = next(
            item
            for item in mounts
            if item["mountPath"] == "/home/akernel/sandboxd/config.toml"
        )
        self.assertEqual(config["name"], "akernel-node-config")
        shared_config = resources[("ConfigMap", "akernel-node-config")]["data"][
            "sandboxd_config.toml"
        ]
        local_config = render()[("ConfigMap", "akernel-node-config")]["data"][
            "sandboxd_config.toml"
        ]
        self.assertEqual(shared_config, local_config)

    def test_existing_rwx_claim_mounts_in_both_roles(self) -> None:
        resources = render(
            "adx.checkpointStorage.kind=distributed_fs",
            "adx.checkpointStorage.existingClaim=juicefs-rwx",
        )
        for kind, name in (
            ("Deployment", "akernel-adx-coordinator"),
            ("DaemonSet", "akernel-node"),
        ):
            pod = resources[(kind, name)]["spec"]["template"]["spec"]
            volume = next(
                item for item in pod["volumes"] if item["name"] == "checkpoint-storage"
            )
            self.assertEqual(
                volume["persistentVolumeClaim"], {"claimName": "juicefs-rwx"}
            )
            self.assertNotIn("hostPath", volume)
            self.assertEqual(
                [
                    m["mountPath"]
                    for m in pod["containers"][0]["volumeMounts"]
                    if m["name"] == "checkpoint-storage"
                ],
                ["/mnt/akernel-shared"],
            )

    def test_invalid_shared_storage_configuration_fails_rendering(self) -> None:
        invalid = (
            ["adx.checkpointStorage.kind=unknown"],
            ["adx.checkpointStorage.kind=distributed_fs"],
            ["adx.checkpointStorage.existingClaim=also-a-claim"],
            ["adx.checkpointStorage.mountPath=/"],
            ["adx.checkpointStorage.mountPath=/home"],
            ["adx.checkpointStorage.mountPath=/home/akernel/adx"],
            ["adx.checkpointStorage.mountPath=/mnt/../etc"],
            ["adx.checkpointStorage.prefix=../checkpoints"],
            ["adx.checkpointStorage.alias=local"],
            ["adx.checkpointStorage.alias=team_cache"],
            ["adx.checkpointStorage.alias="],
            ["adx.checkpointStorage.alias=cache/path"],
            ["adx.checkpointStorage.alias=缓存"],
        )
        for settings in invalid:
            with (
                self.subTest(settings=settings),
                self.assertRaises(subprocess.CalledProcessError),
            ):
                if settings[0] in (
                    "adx.checkpointStorage.kind=unknown",
                    "adx.checkpointStorage.kind=distributed_fs",
                ):
                    render(*settings)
                else:
                    self.shared_resources(*settings)


if __name__ == "__main__":
    unittest.main()
