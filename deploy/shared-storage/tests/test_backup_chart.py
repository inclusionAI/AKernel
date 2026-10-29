# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Validate backup authentication and the narrowly scoped recovery access."""
import copy
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

CHART = Path(__file__).resolve().parents[1] / "backup"


def render(values):
    with tempfile.NamedTemporaryFile(mode="w") as config:
        yaml.safe_dump(values, config)
        config.flush()
        result = subprocess.run(["helm", "template", "akernel-checkpoint-backup", str(CHART),
                                 "-n", "akernel-storage", "-f", config.name],
                                capture_output=True, text=True, check=True)
    return [o for o in yaml.safe_load_all(result.stdout) if o]


class BackupChartTests(unittest.TestCase):
    def values(self):
        return {"enabled": True, "image": "example/tools@sha256:" + "0" * 64,
                "oidc": {"roleArn": "writer", "providerArn": "provider"},
                "config": {"oss": {"bucket": "private-backup", "region": "cn-hongkong",
                                   "endpoint": "https://oss-cn-hongkong-internal.aliyuncs.com"},
                           "recoveryObjects": {"akernel": {"secrets": ["core-auth"]}}}}

    def test_disabled_default_creates_nothing(self):
        self.assertEqual(render({}), [])

    def test_oidc_token_and_recovery_access_are_scoped(self):
        docs = render(self.values())
        job = next(o for o in docs if o["kind"] == "CronJob")["spec"]
        self.assertEqual(job["concurrencyPolicy"], "Forbid")
        pod = job["jobTemplate"]["spec"]["template"]["spec"]
        token = next(v for v in pod["volumes"] if v["name"] == "oidc")
        self.assertEqual(token["projected"]["sources"][0]["serviceAccountToken"]["audience"],
                         "sts.aliyuncs.com")
        role = next(o for o in docs if o["kind"] == "Role")
        self.assertEqual(role["metadata"]["namespace"], "akernel")
        self.assertEqual(role["rules"], [{"apiGroups": [""], "resources": ["secrets"],
                                         "resourceNames": ["core-auth"], "verbs": ["get"]}])
        self.assertFalse(any(o["kind"].startswith("ClusterRole") for o in docs))

    def test_sts_requires_an_explicit_secret(self):
        values = self.values()
        values["config"]["oss"]["credentialsMode"] = "sts"
        with self.assertRaises(subprocess.CalledProcessError):
            render(values)
        values["stsSecret"] = "temporary-sts"
        docs = render(values)
        pod = next(o for o in docs if o["kind"] == "CronJob")["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        self.assertFalse(any(v["name"] == "oidc" for v in pod["volumes"]))

    def test_rejects_unpinned_tools_unbounded_pause_and_implicit_auth(self):
        for path, value in [(('image',), 'example/tools:latest'),
                            (('config', 'pauseMilliseconds'), 46000),
                            (('config', 'oss', 'credentialsMode'), 'default'),
                            (('config', 'oss', 'endpoint'), 'http://oss.invalid'),
                            (('oidc', 'roleArn'), '')]:
            values = copy.deepcopy(self.values())
            target = values
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            with self.assertRaises(subprocess.CalledProcessError):
                render(values)


if __name__ == "__main__":
    unittest.main()
