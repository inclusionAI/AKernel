#!/usr/bin/env python3
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Render production storage layouts and verify failure-domain boundaries."""

import copy
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


def render(chart, values):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as config:
        yaml.safe_dump(values, config)
        config.flush()
        result = subprocess.run([
            "helm", "template", "akernel-storage", str(ROOT / "charts" / chart),
            "--namespace", "akernel-storage", "-f", config.name,
        ], capture_output=True, text=True, check=True)
    return {(item["kind"], item["metadata"]["name"]): item
            for item in yaml.safe_load_all(result.stdout) if item}


class HALayoutTests(unittest.TestCase):
    def shared(self):
        return {
            "ceph": {"storageClass": "independent-essd", "failureDomain": "zone",
                     "zones": ["zone-b", "zone-c", "zone-d"], "osdCount": 3,
                     "osdSize": "1Ti", "mgrCount": 2, "allowedCiphers": ["aes256k"],
                     "nodeSelector": {"node-role": "akernel-storage"},
                     "tolerations": [{"key": "dedicated", "operator": "Equal",
                                      "value": "akernel-storage", "effect": "NoSchedule"}]},
            "postgres": {"mode": "external", "existingSecret": "juicefs-app",
                         "external": {"host": "juicefs-metadata-rw", "caSecret": "juicefs-metadata-ca"}},
        }

    def test_one_raw_osd_per_zone_and_zone_replication(self):
        docs = render("juicefs", self.shared())
        cluster = docs[("CephCluster", "rook-ceph")]["spec"]
        self.assertEqual(cluster["mgr"]["count"], 2)
        self.assertEqual(cluster["security"]["cephx"]["allowedCiphers"], ["aes256k"])
        self.assertEqual(cluster["placement"]["all"]["tolerations"][0]["value"], "akernel-storage")
        sets = cluster["storage"]["storageClassDeviceSets"]
        self.assertEqual(len(sets), 3)
        zones = set()
        for device in sets:
            self.assertEqual(device["count"], 1)
            spec = device["volumeClaimTemplates"][0]["spec"]
            self.assertEqual(spec["volumeMode"], "Block")
            self.assertEqual(spec["resources"]["requests"]["storage"], "1Ti")
            self.assertEqual(device["placement"], device["preparePlacement"])
            for expr in device["placement"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]["matchExpressions"]:
                if expr["key"] == "topology.kubernetes.io/zone":
                    zones.update(expr["values"])
        self.assertEqual(zones, {"zone-b", "zone-c", "zone-d"})
        store = docs[("CephObjectStore", "checkpoints")]["spec"]
        for pool in ["metadataPool", "dataPool"]:
            self.assertEqual(store[pool]["failureDomain"], "zone")
            self.assertEqual(store[pool]["replicated"]["size"], 3)

    def test_external_postgres_uses_tls_in_every_database_consumer(self):
        docs = render("juicefs", self.shared())
        self.assertFalse(any(kind in {"PersistentVolumeClaim", "StatefulSet", "Secret"} for kind, _ in docs))
        pods = [docs[("DaemonSet", "akernel-storage-client")]["spec"]["template"]["spec"]]
        pods += [item["spec"]["template"]["spec"] for (kind, name), item in docs.items()
                 if kind == "Job" and "-format-" in name]
        for pod in pods:
            self.assertTrue(any(v["name"] == "postgres-ca" for v in pod["volumes"]))
            for container in pod.get("initContainers", []) + pod["containers"]:
                meta = next((e for e in container.get("env", []) if e["name"] == "META_URL"), None)
                if meta:
                    self.assertIn("sslmode=verify-full", meta["value"])
                    self.assertIn("juicefs-metadata-rw", meta["value"])
                    self.assertTrue(any(v["name"] == "postgres-ca" for v in container["volumeMounts"]))

    def test_rejects_missing_ca_and_incomplete_zone_set(self):
        for mutate in [
            lambda v: v["postgres"]["external"].update(caSecret=""),
            lambda v: v["ceph"].update(zones=["zone-b", "zone-c"]),
            lambda v: v["ceph"].update(zones=["zone-b", "zone-b", "zone-d"]),
        ]:
            values = self.shared()
            mutate(values)
            with self.assertRaises(subprocess.CalledProcessError):
                render("juicefs", values)

    def test_legacy_single_database_layout_remains_available(self):
        docs = render("juicefs", {"ceph": {"storageClass": "essd"}, "postgres": {"storageClass": "essd"}})
        self.assertEqual(docs[("StatefulSet", "akernel-storage-postgres")]["spec"]["replicas"], 1)

    def test_ha_state_requires_pinned_images_and_independent_pvcs(self):
        image = "example/image@sha256:" + "0" * 64
        values = {"storageClass": "independent-essd", "postgres": {"image": image.replace("image@", "postgresql:17.11@")},
                  "redis": {"image": image, "sentinelImage": image, "exporterImage": image}, "haproxy": {"image": image}}
        docs = render("state-services", values)
        pg = docs[("Cluster", "juicefs-metadata")]["spec"]
        self.assertEqual(pg["instances"], 3)
        self.assertEqual(pg["postgresql"]["synchronous"], {"method": "any", "number": 1, "dataDurability": "required"})
        self.assertEqual(pg["storage"], {"storageClass": "independent-essd", "size": "50Gi"})
        redis = docs[("RedisReplication", "akernel-redis")]["spec"]
        self.assertEqual(redis["clusterSize"], 3)
        self.assertEqual(redis["storage"]["volumeClaimTemplate"]["spec"]["resources"]["requests"]["storage"], "20Gi")
        self.assertEqual(docs[("RedisSentinel", "akernel-redis-sentinel")]["spec"]["clusterSize"], 3)
        self.assertEqual(docs[("Deployment", "akernel-redis-proxy")]["spec"]["replicas"], 2)
        invalid = copy.deepcopy(values)
        invalid["redis"]["image"] = "redis:latest"
        with self.assertRaises(subprocess.CalledProcessError):
            render("state-services", invalid)
        redis_first = copy.deepcopy(values)
        redis_first["postgres"] = {"enabled": False}
        self.assertNotIn(("Cluster", "juicefs-metadata"), render("state-services", redis_first))
        redis_first["storageClass"] = ""
        with self.assertRaises(subprocess.CalledProcessError):
            render("state-services", redis_first)
        invalid_pg = copy.deepcopy(values)
        invalid_pg["postgres"]["image"] = image
        with self.assertRaises(subprocess.CalledProcessError):
            render("state-services", invalid_pg)


if __name__ == "__main__":
    unittest.main()
