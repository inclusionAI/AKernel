"""Admission, LRU, and fenced Registry rotation regressions."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from capacity import CapacityController, CapacityUnavailable
from retention import Conflict, Retention


class CapacityTests(TestCase):
    def setUp(self):
        data = TemporaryDirectory()
        self.addCleanup(data.cleanup)
        root = Path(data.name)
        token = root / "token"
        token.write_text("fixture-token")
        self.database = root / "retention.sqlite"
        self.clock = patch("retention.time.time", return_value=1000).start()
        self.addCleanup(patch.stopall)
        self.config = {
            "adx_origin": "https://api.example.test",
            "runner_token_file": str(token),
            "adx_token_file": str(token),
            "initial_slot": "a",
            "default_ttl_seconds": 600,
            "auto_delete": True,
            "registries": {
                slot: {
                    "origin": f"https://registry-{slot}.example.test",
                    "profile": f"harbor-ephemeral-{slot}",
                }
                for slot in ("a", "b")
            },
            "capacity": {
                "driver": "/unused/capacity-driver",
                "storage_ids": {"a": "volume-a", "b": "volume-b"},
                "drain_seconds": 30,
                "max_in_flight_builds": 3,
                "rotation_interval_seconds": 3600,
                "lru_idle_seconds": 10,
            },
        }
        self.capacity = {"a": 500, "b": 500}
        self.writable = {"a": True, "b": True}
        self.calls = []
        self.open()

    def open(self):
        self.retention = Retention(self.config, self.database)
        self.controller = CapacityController(self.retention)
        self.controller.driver = self.driver

    def driver(self, action, slot):
        if action == "measure":
            return json.dumps({
                "storage_id": "volume-" + slot,
                "total_bytes": 1000,
                "available_bytes": self.capacity[slot],
                "writable": self.writable[slot],
            })
        self.calls.append((action, slot))
        if action == "fence":
            self.writable[slot] = False
        elif action == "gc":
            self.assertFalse(self.writable[slot])
            self.capacity[slot] = 600
        elif action == "restore":
            self.writable[slot] = True
        return ""

    def repository(self, build):
        slot = build["profile"][-1]
        return f"{self.retention.registries[slot].authority}/akernel-ephemeral/{build['id']}/service-0"

    def plan(self, build):
        self.retention.plan(build["id"], self.repository(build) + ":" + build["tag"])

    def publish(self, build):
        slot = build["profile"][-1]
        reference = self.repository(build) + "@sha256:" + "b" * 64
        with patch.object(self.retention.registries[slot], "resolve", return_value=reference):
            receipt = self.retention.commit(build["id"], reference)
        return reference, receipt

    def completed_use(self, reference, name):
        hold = self.retention.acquire(reference, name)["id"]
        instance = "default-" + name
        with patch.object(self.retention, "observe", return_value={"image": reference}):
            self.retention.bind(hold, instance)
        self.retention.finish(hold, instance)

    def test_high_watermark_hysteresis_and_stale_sample(self):
        with self.assertRaises(CapacityUnavailable):
            self.retention.begin()
        for free, admitted in ((500, True), (200, False), (300, False), (350, True)):
            with self.subTest(free=free):
                self.capacity["a"] = free
                self.controller.refresh()
                if admitted:
                    self.retention.begin()
                else:
                    with self.assertRaises(CapacityUnavailable):
                        self.retention.begin()
        self.clock.return_value += 61
        with self.assertRaises(CapacityUnavailable):
            self.retention.begin()

    def test_slot_capacity_cannot_be_rebound(self):
        self.config["capacity"]["storage_ids"]["a"] = "different-volume"
        with self.assertRaisesRegex(ValueError, "durable state"):
            Retention(self.config, self.database)

    def test_invalid_storage_sample_closes_admission(self):
        self.controller.refresh()
        self.retention.begin()
        with patch.object(self.controller, "driver", return_value='{"storage_id":"other"}'):
            self.controller.refresh()
        with self.assertRaises(CapacityUnavailable):
            self.retention.begin()

    def test_burst_plans_are_atomically_bounded(self):
        self.controller.refresh()
        builds = [self.retention.begin() for _ in range(12)]

        def try_plan(build):
            try:
                self.plan(build)
            except CapacityUnavailable:
                return False
            return True

        with ThreadPoolExecutor(max_workers=len(builds)) as workers:
            accepted = list(workers.map(try_plan, builds))
        self.assertEqual(sum(accepted), 3)
        # The successful build releases a write admission without touching its TTL.
        self.publish(builds[accepted.index(True)])
        self.plan(builds[accepted.index(False)])

    def test_pressure_uses_lru_after_expiry_and_preserves_unused_and_live_images(self):
        self.controller.refresh()
        expired = self.retention.begin(4)
        self.plan(expired)
        self.publish(expired)
        builds = [self.retention.begin() for _ in range(4)]
        references = []
        for build in builds:
            self.plan(build)
            reference, receipt = self.publish(build)
            self.assertTrue(receipt["pressure_evictable"])
            references.append(reference)
        old, recent, unused, busy = references
        self.clock.return_value = 1001
        self.completed_use(recent, "recent-first-use")
        self.clock.return_value = 1002
        self.completed_use(old, "old")
        self.clock.return_value = 1005
        self.completed_use(recent, "recent-second-use")
        self.completed_use(busy, "busy-first-use")
        self.retention.acquire(busy, "busy-live-use")
        self.clock.return_value = 1020
        self.capacity["a"] = 200
        self.controller.refresh()
        self.controller.reserve_rotation()
        with patch.object(self.retention, "observe", return_value=None):
            regular = self.retention.sweep(apply=False, slot="a")
            pressured = self.retention.sweep(apply=False, slot="a", pressure=True)
        self.assertEqual([row["id"] for row in regular], [expired["id"]])
        self.assertEqual([row["id"] for row in pressured], [expired["id"], builds[0]["id"], builds[1]["id"]])
        self.retention.acquire(unused, "unused-first-use")
        self.assertNotIn(busy, [row["reference"] for row in pressured])

    def test_lru_claim_cannot_overtake_new_sandbox_pin(self):
        self.controller.refresh()
        build = self.retention.begin()
        self.plan(build)
        reference, _ = self.publish(build)
        self.completed_use(reference, "old")
        self.clock.return_value = 1015
        self.retention.switch("b")

        def reading_manifest(*_):
            self.retention.acquire(reference, "new-live-use")
            return ["sha256:" + "b" * 64]

        registry = self.retention.registries["a"]
        with (
            patch.object(self.retention, "observe", return_value=None),
            patch.object(registry, "worklist", side_effect=reading_manifest),
            patch.object(registry, "delete") as delete,
        ):
            self.retention.sweep(apply=True, slot="a", pressure=True)
        delete.assert_not_called()

    def test_enabling_lru_preserves_previous_receipt_contract(self):
        settings = self.config.pop("capacity")
        self.retention = Retention(self.config, self.database)
        build = self.retention.begin()
        self.plan(build)
        reference, receipt = self.publish(build)
        self.assertFalse(receipt["pressure_evictable"])
        self.completed_use(reference, "strict")
        self.config["capacity"] = settings
        self.open()
        self.clock.return_value += 20
        self.controller.refresh()
        self.retention.switch("b")
        with patch.object(self.retention, "observe", return_value=None):
            self.assertEqual(self.retention.sweep(apply=False, slot="a", pressure=True), [])
        self.retention.acquire(reference, "strict-second-use")

    def test_capacity_rotation_drains_existing_write_and_keeps_standby_writable(self):
        self.controller.refresh()
        old = self.retention.begin()
        self.plan(old)
        self.capacity["a"] = 190
        self.controller.refresh()
        self.controller.cycle()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.retention.begin()["profile"], "harbor-ephemeral-b")
        self.publish(old)
        original = self.controller.driver

        def during_gc(action, slot):
            if action == "gc":
                new = self.retention.begin()
                self.assertEqual(new["profile"], "harbor-ephemeral-b")
                self.plan(new)
            return original(action, slot)

        self.controller.driver = during_gc
        self.controller.cycle()
        self.assertEqual(self.calls, [("fence", "a"), ("gc", "a"), ("restore", "a")])
        self.assertIsNone(self.retention.status()["maintenance"]["slot"])
        self.assertEqual(self.retention.status()["slots"][0]["state"], "ready")

    def test_restore_retry_does_not_recollect_writable_storage(self):
        self.controller.refresh()
        self.capacity["a"] = 200
        self.controller.refresh()

        def disconnect_after_restore(action, slot):
            result = self.driver(action, slot)
            if action == "restore":
                raise OSError("lost restore response")
            return result

        self.controller.driver = disconnect_after_restore
        with self.assertRaises(OSError):
            self.controller.cycle()
        self.open()
        self.controller.cycle()
        self.assertEqual(self.calls.count(("gc", "a")), 1)
        self.assertEqual(self.calls.count(("restore", "a")), 2)
        self.assertIsNone(self.retention.status()["maintenance"]["slot"])

    def test_fence_must_be_observed_before_gc(self):
        self.controller.refresh()
        self.capacity["a"] = 200
        self.controller.refresh()

        def ineffective_driver(action, slot):
            return "" if action == "fence" else self.driver(action, slot)

        self.controller.driver = ineffective_driver
        with self.assertRaisesRegex(ValueError, "fenced"):
            self.controller.cycle()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.retention.status()["slots"][0]["state"], "draining")

    def test_periodic_maintenance_and_recovery_when_both_volumes_are_full(self):
        self.controller.refresh()
        self.clock.return_value += 3600
        self.controller.refresh()
        self.controller.cycle()
        self.assertEqual(self.calls[0], ("fence", "a"))
        self.capacity = {"a": 100, "b": 100}
        self.controller.refresh()
        with self.assertRaises(CapacityUnavailable):
            self.retention.begin()
        self.clock.return_value += 300
        self.controller.refresh()
        self.controller.cycle()
        self.assertEqual(self.calls.count(("gc", "a")), 2)
        self.controller.cycle()
        self.assertEqual(self.calls.count(("gc", "b")), 1)
        self.assertEqual(self.retention.begin()["profile"], "harbor-ephemeral-a")
