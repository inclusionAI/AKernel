#!/usr/bin/env python3
"""Rendered admission policy for AKernel execution nodes."""

import unittest

import yaml

from test_adx_chart import render


def configs(values=None, *, umbrella=False):
    resources, _ = render(values=values, umbrella=umbrella)
    data = next(r for r in resources if r["kind"] == "ConfigMap"
                and r["metadata"]["name"] == "akernel-adx-config")["data"]
    return {
        service["role"]: service.get("config") or {}
        for filename in ("node.yaml", "coordinator.yaml", "ingress-api.yaml")
        for service in yaml.safe_load(data[filename])["services"]
    }


class AdxPressureTest(unittest.TestCase):
    def test_core_and_umbrella_disable_node_pressure_by_default(self):
        for umbrella in (False, True):
            with self.subTest(umbrella=umbrella):
                roles = configs(umbrella=umbrella)
                self.assertEqual(roles["adxlet"].get("pressure"), {
                    "memory_enabled": False, "disk_enabled": False,
                })
                for role in ("coordinator", "apiserver", "ingress"):
                    self.assertNotIn("pressure", roles[role])

    def test_operator_can_enable_memory_sampling(self):
        policy = {"memory_enabled": True, "disk_enabled": False}
        for umbrella in (False, True):
            values = {"adx": {"node": {"pressure": policy}}}
            if umbrella:
                values = {"core": values}
            with self.subTest(umbrella=umbrella):
                self.assertEqual(configs(values, umbrella=umbrella)["adxlet"].get("pressure"), policy)

    def test_memory_thresholds_reach_the_node(self):
        policy = {
            "memory_enabled": True, "disk_enabled": False,
            "thresholds": {"memory_high_percent": 95, "memory_low_percent": 85},
        }
        roles = configs({"adx": {"node": {"pressure": policy}}})
        self.assertEqual(roles["adxlet"].get("pressure"), policy)


if __name__ == "__main__":
    unittest.main()
