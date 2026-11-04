#!/usr/bin/env python3

"""Performance resource contract for the combined ADX Ingress/API Server."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

import yaml

CHART = Path(__file__).resolve().parents[1]


class AdxPerformanceResourcesTest(unittest.TestCase):
    def test_ingress_api_has_create_burst_cpu_budget(self) -> None:
        rendered = subprocess.run(
            ["helm", "template", "akernel", str(CHART), "--namespace", "akernel"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        deployment = next(
            item
            for item in yaml.safe_load_all(rendered)
            if item
            and item.get("kind") == "Deployment"
            and item["metadata"]["name"] == "akernel-adx-ingress-api"
        )
        resources = deployment["spec"]["template"]["spec"]["containers"][0]["resources"]
        self.assertEqual(resources["requests"]["cpu"], "2")
        self.assertEqual(resources["limits"]["cpu"], "8")


if __name__ == "__main__":
    unittest.main()
