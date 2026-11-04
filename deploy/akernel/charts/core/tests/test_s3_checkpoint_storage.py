# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Object checkpoint identity, credential scope and native local restore inputs."""
import subprocess
import tomllib
import unittest

import yaml
from test_shared_checkpoint_storage import render


class S3CheckpointStorageTests(unittest.TestCase):
    def test_maintenance_retains_public_service_without_live_endpoints(self):
        settings = ("adx.ingressApi.publicService.enabled=true",)
        service = ("Service", "akernel-adx-public")
        active = render(*settings)[service]
        held = render(*settings, "adx.ingressApi.publicService.maintenance=true")[service]
        self.assertEqual(held["spec"]["ports"], active["spec"]["ports"])
        self.assertEqual(held["spec"]["type"], active["spec"]["type"])
        self.assertEqual(held["spec"]["selector"]["akernel.io/maintenance"], "true")
        self.assertNotIn("akernel.io/maintenance", active["spec"]["selector"])
        ingress = render(*settings)[("Deployment", "akernel-adx-ingress-api")]
        self.assertNotIn("akernel.io/maintenance", ingress["spec"]["template"]["metadata"]["labels"])

    def s3(self, *settings):
        return render("adx.checkpointStorage.kind=s3",
                      "adx.checkpointStorage.s3.bucket=checkpoint-bucket",
                      "adx.checkpointStorage.s3.region=us-east-1",
                      "adx.checkpointStorage.s3.endpoint=https://rgw.example.com",
                      "adx.checkpointStorage.s3.credentialsSecret=checkpoint-credentials",
                      *settings)

    def test_node_cache_and_reclaimer_share_identity_without_fuse_mounts(self):
        docs = self.s3()
        configs = docs[("ConfigMap", "akernel-adx-config")]["data"]
        node = yaml.safe_load(configs["node.yaml"])["services"][0]["config"]
        coordinator = yaml.safe_load(configs["coordinator.yaml"])["services"][0]["config"]
        reclaim = coordinator["checkpoint_reclaimers"][0]
        capture = node["checkpoint_storage"]
        self.assertEqual(capture["kind"], "s3")
        self.assertEqual(capture["root"], "/home/akernel/adx/checkpoint-s3-cache")
        self.assertEqual(capture["cache_budget_bytes"], 53687091200)
        self.assertEqual(capture["cache_max_entries"], 500)
        self.assertEqual(reclaim, {k: v for k, v in capture.items()
                                  if k not in ["root", "cache_budget_bytes", "cache_max_entries"]})
        for key in [("DaemonSet", "akernel-node"), ("Deployment", "akernel-adx-coordinator")]:
            pod = docs[key]["spec"]["template"]["spec"]
            self.assertFalse(any(v["name"] == "checkpoint-storage" for v in pod["volumes"]))

    def test_object_credentials_only_reach_node_and_coordinator(self):
        docs = self.s3()
        for key in [("DaemonSet", "akernel-node"), ("Deployment", "akernel-adx-coordinator")]:
            env = docs[key]["spec"]["template"]["spec"]["containers"][0]["env"]
            indexed = {e["name"]: e for e in env}
            for name in ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"]:
                self.assertEqual(indexed[name]["valueFrom"]["secretKeyRef"],
                                 {"name": "checkpoint-credentials", "key": name})
            self.assertEqual(indexed["AWS_EC2_METADATA_DISABLED"]["value"], "true")
        ingress = docs[("Deployment", "akernel-adx-ingress-api")]["spec"]["template"]["spec"]
        self.assertFalse(any(e["name"].startswith("AWS_") for e in ingress["containers"][0]["env"]))

    def test_native_reflink_checks_s3_cache_without_shared_filesystem(self):
        docs = self.s3("node.config.sandboxd.nativeFilestore=true",
                       "node.config.sandboxd.runscCheckpointMode=reflink")
        runtime = tomllib.loads(docs[("ConfigMap", "akernel-node-config")]["data"]["sandboxd_config.toml"])["plugin"]["runtime"]
        self.assertEqual(runtime["filestore_dir_size"], "")
        self.assertEqual(runtime["runsc"]["checkpoint_mode"], "reflink")
        self.assertTrue(runtime["runsc"]["background_restore"])
        self.assertTrue(runtime["checkpoint_sync"]["sync_output"])
        pod = docs[("DaemonSet", "akernel-node")]["spec"]["template"]["spec"]
        init = next(i for i in pod["initContainers"] if i["name"] == "prepare-checkpoint-storage")
        self.assertIn("--local-cache-root", init["args"])
        self.assertNotIn("--mount-root", init["args"])
        self.assertFalse(next(v for v in init["volumeMounts"] if v["name"] == "home-disk")["readOnly"])

    def test_local_reflink_uses_the_existing_local_checkpoint_directory(self):
        docs = render("node.config.sandboxd.nativeFilestore=true",
                      "node.config.sandboxd.runscCheckpointMode=reflink")
        pod = docs[("DaemonSet", "akernel-node")]["spec"]["template"]["spec"]
        init = next(i for i in pod["initContainers"] if i["name"] == "prepare-checkpoint-storage")
        self.assertIn("/home/akernel/adx/checkpoints", init["args"])
        self.assertNotIn("--mount-root", init["args"])

    def test_rejects_incomplete_identity_unsafe_cache_and_implicit_http(self):
        for value in ["s3.bucket=", "s3.region=", "s3.credentialsSecret=",
                      "s3.endpoint=http://rgw.example.com", "s3.cacheRoot=/tmp/checkpoints",
                      "s3.cacheRoot=/home/akernel/adx/run/cache", "s3.cacheBudgetBytes=0",
                      "s3.cacheMaxEntries=1.5", "alias=local", "prefix=../checkpoints"]:
            with self.subTest(value=value), self.assertRaises(subprocess.CalledProcessError):
                self.s3("adx.checkpointStorage." + value)
        self.s3("adx.checkpointStorage.s3.endpoint=http://rgw.example.com",
                "adx.checkpointStorage.s3.allowHttp=true")
        self.s3("adx.checkpointStorage.s3.endpoint=")


if __name__ == "__main__":
    unittest.main()
