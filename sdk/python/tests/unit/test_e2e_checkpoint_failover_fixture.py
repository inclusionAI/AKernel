"""Keep the opt-in checkpoint failover E2E's state assertions strict."""

import unittest

from tests.e2e.fault import test_checkpoint_failover as fixture


def record(*, storage="shared", node="node-a", generation=1, state="Running"):
    return {
        "assignment": {"node_id": node, "generation": generation},
        "result": {
            "state": state,
            "resources_held": state == "Running",
            "checkpoint": {
                "id": "checkpoint-a",
                "artifact": {
                    "storage": storage,
                    "location": "artifact-a",
                    "size_bytes": 10,
                },
            },
        },
    }


class CheckpointFailoverFixtureTests(unittest.TestCase):
    def test_shared_fault_targets_the_actual_owner(self):
        self.assertEqual(
            fixture.shared_failover_pair(record(node="node-a"), "node-a", "node-b"),
            ("node-a", "node-b"),
        )
        self.assertEqual(
            fixture.shared_failover_pair(record(node="node-b"), "node-a", "node-b"),
            ("node-b", "node-a"),
        )
        with self.assertRaisesRegex(AssertionError, "candidate"):
            fixture.shared_failover_pair(record(node="node-c"), "node-a", "node-b")
        pinned = record(node="node-a")
        pinned["spec"] = {
            "scheduling": {
                "placement_groups": [{"target": "node", "required": True}]
            }
        }
        with self.assertRaisesRegex(AssertionError, "hard node"):
            fixture.shared_failover_pair(pinned, "node-a", "node-b")

    def test_shared_point_requires_remote_artifact(self):
        self.assertEqual(
            fixture.require_checkpoint_storage(record(), "shared"), "checkpoint-a"
        )
        with self.assertRaisesRegex(AssertionError, "storage"):
            fixture.require_checkpoint_storage(record(storage="local"), "shared")

    def test_shared_restore_requires_new_generation_and_healthy_owner(self):
        self.assertTrue(
            fixture.restored_on_healthy(
                record(node="node-b", generation=2), "node-b", 1
            )
        )
        self.assertFalse(
            fixture.restored_on_healthy(
                record(node="node-a", generation=2), "node-b", 1
            )
        )
        self.assertFalse(
            fixture.restored_on_healthy(
                record(node="node-b", generation=1), "node-b", 1
            )
        )

    def test_local_only_failure_must_not_relocate_or_hold_resources(self):
        failed = record(storage="local", state="Failed")
        self.assertTrue(fixture.local_only_failed(failed, "node-a", 1))
        failed["assignment"]["node_id"] = "node-b"
        self.assertFalse(fixture.local_only_failed(failed, "node-a", 1))
        failed["assignment"]["node_id"] = "node-a"
        failed["result"]["resources_held"] = True
        self.assertFalse(fixture.local_only_failed(failed, "node-a", 1))


if __name__ == "__main__":
    unittest.main()
