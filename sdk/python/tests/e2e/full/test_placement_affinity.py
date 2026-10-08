# Copyright (c) 2026 Ant Group Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Verify requested node and runtime placement against persisted ownership."""

import json
import os
import unittest
import uuid

from akernel_sdk import BackendOperationError, Sandbox, resources
from tests.e2e.full.test_redis_lifecycle import _Redis

_ENABLED = (
    os.environ.get("AKERNEL_RUN_INTEGRATION") == "1"
    and bool(os.environ.get("AKERNEL_SERVER_ADDRESS"))
    and bool(os.environ.get("AKERNEL_TOKEN"))
    and bool(os.environ.get("AKERNEL_REDIS_HOST"))
)
_RUNTIME = os.environ.get("AKERNEL_TEST_RUNTIME", "runsc")


def select_runtime_pair(eligible, records, runtime):
    """Find two live nodes whose sandboxd inventories differ for a runtime."""
    capable = []
    incapable = []
    for node_id in eligible:
        record = records.get(node_id) or {}
        node = record.get("node") or {}
        session = record.get("session") or {}
        if not node.get("available") or not session.get("routable"):
            continue
        if "runtime_classes" not in node:
            continue
        classes = node["runtime_classes"]
        (capable if runtime in classes else incapable).append(node_id)
    return (capable[0], incapable[0]) if capable and incapable else None


def require_unassigned_rejection(record):
    """Check the failed create before cleanup can alter its persisted state."""
    if record is None:
        return
    result = record.get("result") or {}
    assert not result.get("resources_held"), "rejected runtime held resources"
    assert not record.get("assignment"), "rejected runtime acquired ownership"


@unittest.skipUnless(_ENABLED, "requires deployed SDK endpoints and test Redis access")
class PlacementAffinityIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.redis = _Redis()
        namespace = os.environ.get("AKERNEL_REDIS_NAMESPACE", "akernel")
        cls.control_key = f"adx:{{{namespace}}}:control:v1"

    @classmethod
    def tearDownClass(cls):
        cls.redis.close()

    def _assigned_node(self, sandbox_id: str) -> str:
        value = self.redis.call("HGET", self.control_key, f"environment:{sandbox_id}")
        self.assertIsNotNone(value, "created sandbox has no persisted ownership")
        record = json.loads(value)
        self.assertEqual(record["result"]["state"], "Running")
        self.assertTrue(record["result"]["resources_held"])
        return record["assignment"]["node_id"]

    def _eligible_nodes(self):
        return [
            node.id
            for node in resources()
            if node.allocatable.get("CPU", 0) >= 1000
            and node.allocatable.get("Memory", 0) >= 2048
        ]

    def _node_records(self, node_ids):
        records = {}
        for node_id in node_ids:
            raw = self.redis.call("HGET", self.control_key, f"node:{node_id}")
            if raw is not None:
                records[node_id] = json.loads(raw)
        return records

    def test_node_pin_matches_both_persisted_assignments(self):
        eligible = self._eligible_nodes()
        if len(eligible) < 2:
            self.skipTest(f"requires two eligible nodes; found {eligible}")
        first_node, second_node = eligible[:2]
        with (
            Sandbox(
                runtime=_RUNTIME, node_id=first_node, cpu=1000, memory=2048
            ) as first,
            Sandbox(
                runtime=_RUNTIME, node_id=second_node, cpu=1000, memory=2048
            ) as second,
        ):
            self.assertEqual(self._assigned_node(first.id), first_node)
            self.assertEqual(self._assigned_node(second.id), second_node)
            self.assertEqual(first.commands.run("printf first").stdout, "first")
            self.assertEqual(second.commands.run("printf second").stdout, "second")

    def test_runtime_selects_only_capable_node(self):
        runtime = os.environ.get("AKERNEL_TEST_RUNTIME_CLASS", _RUNTIME).strip()
        eligible = self._eligible_nodes()
        records = self._node_records(eligible)
        pair = select_runtime_pair(eligible, records, runtime)
        if pair is None:
            reporting_inventory = [
                node_id
                for node_id in eligible
                if "runtime_classes" in (records.get(node_id, {}).get("node") or {})
            ]
            reason = (
                f"requires live nodes with different sandboxd {runtime} inventories; "
                f"eligible nodes: {eligible}; "
                f"nodes reporting inventory: {reporting_inventory}"
            )
            if os.environ.get("AKERNEL_REQUIRE_RUNTIME_AFFINITY") == "1":
                self.fail(reason)
            self.skipTest(reason)
        capable, incapable = pair
        requested_capable = os.environ.get("AKERNEL_TEST_RUNTIME_NODE_ID", "").strip()
        requested_incapable = os.environ.get(
            "AKERNEL_TEST_RUNTIME_INCOMPATIBLE_NODE_ID", ""
        ).strip()
        if requested_capable or requested_incapable:
            self.assertTrue(
                requested_capable and requested_incapable,
                "both explicit runtime fixture node IDs are required",
            )
            self.assertIn(requested_capable, eligible)
            self.assertIn(requested_incapable, eligible)
            self.assertIn(
                runtime,
                records[requested_capable]["node"]["runtime_classes"],
            )
            self.assertNotIn(
                runtime,
                records[requested_incapable]["node"]["runtime_classes"],
            )
            capable, incapable = requested_capable, requested_incapable
        capable_nodes = {
            node_id
            for node_id in eligible
            if runtime
            in (records.get(node_id, {}).get("node") or {}).get("runtime_classes", [])
        }
        image = os.environ.get("AKERNEL_TEST_RUNTIME_IMAGE") or None
        kwargs = {
            "runtime": runtime,
            "image": image,
            "cpu": 1000,
            "memory": 2048,
            "schedule_timeout": 3,
        }
        with Sandbox(node_id=capable, **kwargs) as reference:
            self.assertEqual(self._assigned_node(reference.id), capable)
            self.assertEqual(reference.commands.run("printf ready").stdout, "ready")

        rejected_name = f"e2e-runtime-reject-{uuid.uuid4().hex[:12]}"
        rejected_id = f"default-{rejected_name}"
        try:
            with (
                self.assertRaises(BackendOperationError),
                Sandbox(
                    name=rejected_name,
                    detached=True,
                    node_id=incapable,
                    **kwargs,
                ),
            ):
                self.fail("runtime was admitted on an incompatible node")
        finally:
            try:
                raw = self.redis.call(
                    "HGET", self.control_key, f"environment:{rejected_id}"
                )
                require_unassigned_rejection(
                    json.loads(raw) if raw is not None else None
                )
            finally:
                try:
                    Sandbox.delete(rejected_name)
                except BackendOperationError:
                    pass

        for _ in range(2):
            with Sandbox(**kwargs) as selected:
                self.assertIn(self._assigned_node(selected.id), capable_nodes)
                self.assertEqual(
                    selected.commands.run("printf placed").stdout, "placed"
                )


if __name__ == "__main__":
    unittest.main()
