# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Readiness and host-coverage regressions for the OnDelete mount clients."""

import copy
import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import yaml

DEPLOY = Path(__file__).resolve().parents[4]
STORAGE = DEPLOY / "shared-storage/charts/juicefs"
SPEC = importlib.util.spec_from_file_location(
    "readiness", STORAGE / "scripts/wait_for_clients.py"
)
READINESS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(READINESS)


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.ds = {
            "metadata": {"generation": 2, "uid": "current-ds"},
            "status": {
                "observedGeneration": 2,
                "desiredNumberScheduled": 2,
                "updatedNumberScheduled": 2,
                "numberReady": 2,
                "numberAvailable": 2,
            },
        }
        self.nodes = [
            {"metadata": {"name": node}} for node in ("worker", "coordinator")
        ]
        self.pods = [
            {
                "metadata": {
                    "ownerReferences": [{"uid": "current-ds", "controller": True}]
                },
                "spec": {"nodeName": node},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
            }
            for node in ("worker", "coordinator")
        ]

    def test_ready_current_clients_cover_both_consumer_hosts(self):
        self.assertEqual(READINESS.pending(self.ds, self.nodes, self.pods), "")

    def test_empty_selector_match_never_succeeds(self):
        ds = copy.deepcopy(self.ds)
        ds["status"] = {}
        self.assertIn("No nodes", READINESS.pending(ds, [], []))

    def test_old_ondelete_template_does_not_pass_even_if_ready(self):
        self.ds["status"]["updatedNumberScheduled"] = 1
        self.assertIn("drain", READINESS.pending(self.ds, self.nodes, self.pods))

    def test_controller_must_observe_current_generation(self):
        self.ds["status"]["observedGeneration"] = 1
        self.assertIn("generation", READINESS.pending(self.ds, self.nodes, self.pods))

    def test_new_consumer_host_must_be_covered(self):
        self.nodes.append({"metadata": {"name": "new-worker"}})
        self.assertIn("coverage", READINESS.pending(self.ds, self.nodes, self.pods))

    def test_missing_unready_terminating_and_unrelated_pods_cannot_cover_host(self):
        for mutation in ("missing", "unready", "terminating", "unrelated"):
            with self.subTest(mutation=mutation):
                pods = copy.deepcopy(self.pods)
                if mutation == "missing":
                    pods.pop()
                elif mutation == "unready":
                    pods[-1]["status"]["conditions"][0]["status"] = "False"
                elif mutation == "terminating":
                    pods[-1]["metadata"]["deletionTimestamp"] = "now"
                else:
                    pods[-1]["metadata"]["ownerReferences"][0]["uid"] = "old-ds"
                self.assertIn(
                    "coordinator", READINESS.pending(self.ds, self.nodes, pods)
                )

    def test_status_alone_does_not_prove_mount_coverage(self):
        self.pods[-1]["spec"]["nodeName"] = "worker"
        self.assertIn("coordinator", READINESS.pending(self.ds, self.nodes, self.pods))

    def test_unavailable_current_clients_block_release(self):
        self.ds["status"]["numberAvailable"] = 1
        self.assertIn("available", READINESS.pending(self.ds, self.nodes, self.pods))

    def test_wait_retries_until_ready(self):
        check = Mock(side_effect=["mount missing", ""])
        sleep = Mock()
        READINESS.wait(check, 10, now=Mock(side_effect=[0, 1]), sleep=sleep)
        sleep.assert_called_once_with(5)
        self.assertEqual(check.call_count, 2)

    def test_wait_timeout_retains_last_diagnostic(self):
        check = Mock(return_value="missing coordinator mount")
        sleep = Mock()
        with self.assertRaisesRegex(TimeoutError, "missing coordinator mount"):
            READINESS.wait(check, 2, now=Mock(side_effect=[0, 1, 2]), sleep=sleep)
        sleep.assert_called_once_with(1)

    def test_paginated_api_keeps_node_selector(self):
        client = READINESS.Kubernetes.__new__(READINESS.Kubernetes)
        client.get = Mock(
            side_effect=[
                {"items": ["worker"], "metadata": {"continue": "next/token"}},
                {"items": ["coordinator"], "metadata": {}},
            ]
        )
        self.assertEqual(
            client.items("/api/v1/nodes?labelSelector=pool%3Dworkers"),
            ["worker", "coordinator"],
        )
        self.assertEqual(
            client.get.call_args.args[0],
            "/api/v1/nodes?labelSelector=pool%3Dworkers&continue=next%2Ftoken",
        )

    def test_completed_format_does_not_bypass_unready_mount_clients(self):
        # The prerequisite reads mount client readiness independently of the
        # normal format Job that Helm has already finished waiting for.
        ds = copy.deepcopy(self.ds)
        ds["spec"] = {"template": {"spec": {"nodeSelector": {"pool": "consumers"}}}}
        client = Mock()
        client.get.return_value = ds
        client.items.side_effect = [self.nodes, []]

        def verify(check, timeout):
            self.assertIn("coordinator", check())
            self.assertEqual(timeout, 10)

        with (
            patch.object(READINESS, "Kubernetes", return_value=client),
            patch.object(READINESS, "wait", side_effect=verify),
            patch.dict(
                READINESS.os.environ,
                {
                    "POD_NAMESPACE": "storage",
                    "CLIENT_DAEMONSET": "clients",
                    "READINESS_TIMEOUT_SECONDS": "10",
                },
            ),
        ):
            READINESS.main()
        self.assertEqual(
            client.items.call_args_list[0].args[0],
            "/api/v1/nodes?labelSelector=pool%3Dconsumers",
        )

    def test_api_error_is_not_treated_as_ready_or_logged_with_response_body(self):
        client = Mock()
        client.get.side_effect = HTTPError(
            "https://kubernetes.default.svc", 403, "private response", {}, None
        )

        def verify(check, timeout):
            reason = check()
            self.assertEqual(reason, "Cannot verify JuiceFS mount readiness: HTTPError")

        with (
            patch.object(READINESS, "Kubernetes", return_value=client),
            patch.object(READINESS, "wait", side_effect=verify),
            patch.dict(
                READINESS.os.environ,
                {
                    "POD_NAMESPACE": "storage",
                    "CLIENT_DAEMONSET": "clients",
                    "READINESS_TIMEOUT_SECONDS": "10",
                },
            ),
        ):
            READINESS.main()

    def test_chart_hook_blocks_install_and_upgrade_without_replacing_clients(self):
        rendered = subprocess.run(
            [
                "helm",
                "template",
                "akernel-juicefs",
                str(STORAGE),
                "--namespace",
                "storage-test",
                "--set",
                "ceph.storageClass=block",
                "--set",
                "postgres.storageClass=filesystem",
                "--set",
                "juicefs.nodeSelector.pool=consumers",
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        resources = {
            (obj["kind"], obj["metadata"]["name"]): obj
            for obj in yaml.safe_load_all(rendered.stdout)
            if obj
        }
        ds = resources[("DaemonSet", "akernel-juicefs-client")]
        self.assertEqual(ds["spec"]["updateStrategy"], {"type": "OnDelete"})
        self.assertEqual(
            ds["spec"]["template"]["spec"]["nodeSelector"],
            {"kubernetes.io/os": "linux", "pool": "consumers"},
        )
        job = resources[("Job", "akernel-juicefs-readiness")]
        self.assertEqual(
            job["metadata"]["annotations"]["helm.sh/hook"], "post-install,post-upgrade"
        )
        self.assertNotIn(
            "hook-failed", job["metadata"]["annotations"]["helm.sh/hook-delete-policy"]
        )
        self.assertEqual(job["spec"]["activeDeadlineSeconds"], 940)
        self.assertEqual(job["spec"]["backoffLimit"], 0)
        pod = job["spec"]["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], "akernel-juicefs-readiness")
        script = resources[("ConfigMap", "akernel-juicefs-readiness")]["data"][
            "wait_for_clients.py"
        ]
        self.assertEqual(
            script.strip(),
            (STORAGE / "scripts/wait_for_clients.py").read_text().strip(),
        )
        for obj in resources.values():
            if obj["kind"] in ("Role", "ClusterRole"):
                for rule in obj["rules"]:
                    self.assertTrue(set(rule["verbs"]) <= {"get", "list"})
                    self.assertNotIn("secrets", rule["resources"])


if __name__ == "__main__":
    unittest.main()
