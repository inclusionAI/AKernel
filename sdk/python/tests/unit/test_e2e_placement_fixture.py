"""Fixture selection for real runtime-affinity end-to-end tests."""

import os
import unittest
from unittest.mock import patch

from tests.e2e.full import test_placement_affinity as placement

select_runtime_pair = placement.select_runtime_pair


class RuntimePairSelectionTest(unittest.TestCase):
    def test_rejected_runtime_cannot_hold_or_own_resources(self):
        placement.require_unassigned_rejection(None)
        placement.require_unassigned_rejection(
            {"result": {"state": "Failed", "resources_held": False}, "assignment": None}
        )
        for record in (
            {"result": {"resources_held": True}, "assignment": None},
            {"result": {"resources_held": False}, "assignment": {"node_id": "node1"}},
        ):
            with self.subTest(record=record), self.assertRaises(AssertionError):
                placement.require_unassigned_rejection(record)

    def test_selects_capable_and_incapable_routable_nodes(self):
        records = {
            "runsc-node": {
                "node": {"available": True, "runtime_classes": ["runsc"]},
                "session": {"routable": True},
            },
            "runc-node": {
                "node": {"available": True, "runtime_classes": ["runc"]},
                "session": {"routable": True},
            },
        }
        self.assertEqual(
            select_runtime_pair(["runsc-node", "runc-node"], records, "runsc"),
            ("runsc-node", "runc-node"),
        )

    def test_skips_homogeneous_or_unregistered_nodes(self):
        records = {
            "a": {
                "node": {"available": True, "runtime_classes": ["runsc"]},
                "session": {"routable": True},
            },
            "b": {
                "node": {"available": True, "runtime_classes": ["runsc"]},
                "session": {"routable": True},
            },
        }
        self.assertIsNone(select_runtime_pair(["a", "b"], records, "runsc"))
        self.assertIsNone(select_runtime_pair(["a", "missing"], records, "runsc"))

    def test_ignores_unavailable_or_non_routable_nodes(self):
        records = {
            "ready": {
                "node": {"available": True, "runtime_classes": ["runsc"]},
                "session": {"routable": True},
            },
            "lost": {
                "node": {"available": True, "runtime_classes": ["runc"]},
                "session": {"routable": False},
            },
        }
        self.assertIsNone(select_runtime_pair(["ready", "lost"], records, "runsc"))

    def test_missing_inventory_is_unknown_not_incompatible(self):
        records = {
            "ready": {
                "node": {"available": True, "runtime_classes": ["runsc"]},
                "session": {"routable": True},
            },
            "old-record": {
                "node": {"available": True},
                "session": {"routable": True},
            },
        }
        self.assertIsNone(
            select_runtime_pair(["ready", "old-record"], records, "runsc")
        )

    def test_formal_runtime_gate_fails_when_inventory_is_missing(self):
        case = placement.PlacementAffinityIntegrationTest(
            "test_runtime_selects_only_capable_node"
        )
        case._eligible_nodes = lambda: ["old-record"]
        case._node_records = lambda _: {
            "old-record": {
                "node": {"available": True},
                "session": {"routable": True},
            }
        }
        with (
            patch.dict(os.environ, {"AKERNEL_REQUIRE_RUNTIME_AFFINITY": "1"}),
            self.assertRaisesRegex(AssertionError, "nodes reporting inventory: \\[\\]"),
        ):
            case.test_runtime_selects_only_capable_node()


if __name__ == "__main__":
    unittest.main()
