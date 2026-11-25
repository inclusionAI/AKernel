"""Admission, LRU, and fenced Registry rotation regressions."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from capacity import CapacityController, CapacityUnavailable
from retention import Conflict, Retention, main as retention_cli


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
                "prepare_seconds": 60,
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

    def driver(self, action, slot, *, payload=None):
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
        if payload is not None:
            return json.dumps({"cleaned_repositories": payload["abandoned_repositories"]})
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

    def test_burst_is_atomically_bounded_before_preparation(self):
        self.controller.refresh()

        def try_begin(_):
            try:
                return self.retention.begin()
            except CapacityUnavailable:
                return None

        with ThreadPoolExecutor(max_workers=12) as workers:
            builds = [build for build in workers.map(try_begin, range(12)) if build]
        self.assertEqual(len(builds), 3)
        for build in builds:
            self.plan(build)
        with self.assertRaises(CapacityUnavailable):
            self.retention.begin()
        # The successful build releases a write admission without touching its TTL.
        self.publish(builds[0])
        self.plan(self.retention.begin())

    def test_preparation_expiry_and_cancellation_release_only_unused_places(self):
        self.controller.refresh()
        cancelled, carries_write, expires = (self.retention.begin() for _ in range(3))
        self.plan(carries_write)
        self.retention.cancel_build(carries_write["id"])
        with self.assertRaises(CapacityUnavailable):
            self.retention.begin()
        self.retention.cancel_build(cancelled["id"])
        self.retention.begin()
        self.clock.return_value = expires["prepare_until"]
        self.controller.refresh()
        self.retention.begin()
        with self.assertRaisesRegex(CapacityUnavailable, "reservation expired"):
            self.plan(expires)
        self.publish(carries_write)

    def test_pressure_uses_lru_after_expiry_and_preserves_unused_and_live_images(self):
        self.controller.refresh()
        expired = self.retention.begin(4)
        self.plan(expired)
        self.publish(expired)
        builds = []
        references = []
        for _ in range(4):
            build = self.retention.begin()
            builds.append(build)
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

    def test_read_only_registry_still_requires_explicit_fence_before_gc(self):
        self.capacity["a"] = 200
        self.writable["a"] = False
        self.controller.refresh()
        self.controller.cycle()
        self.assertEqual(self.calls, [("fence", "a"), ("gc", "a"), ("restore", "a")])

    def test_retry_can_repair_endpoint_interrupted_by_fence(self):
        self.controller.refresh()
        self.capacity["a"] = 200
        self.controller.refresh()
        interrupted = False

        def interrupt_once(action, slot):
            nonlocal interrupted
            if slot == "a" and action == "fence" and not interrupted:
                interrupted = True
                raise OSError("read-only endpoint still starting")
            if slot == "a" and action == "measure" and interrupted and ("fence", "a") not in self.calls:
                raise OSError("interrupted Registry is unavailable")
            return self.driver(action, slot)

        self.controller.driver = interrupt_once
        with self.assertRaises(OSError):
            self.controller.cycle()
        self.controller.cycle()
        self.assertEqual(self.calls, [("fence", "a"), ("gc", "a"), ("restore", "a")])

    def test_manual_cli_defers_to_queued_maintenance_with_or_without_capacity(self):
        self.capacity["a"] = 200
        self.controller.refresh()
        self.controller.reserve_rotation()
        for config in (self.config, {**self.config, "capacity": None}):
            path = self.database.with_name("config.json")
            path.write_text(json.dumps(config))
            with (
                self.subTest(capacity=config["capacity"] is not None),
                patch("sys.argv", [
                    "retention.py", "--config", str(path), "--database", str(self.database),
                    "seal", "a", "--writers-fenced",
                ]),
                self.assertRaisesRegex(ValueError, "queued Registry maintenance"),
            ):
                retention_cli()

    def test_plan_replay_is_rejected_after_automatic_drain_window(self):
        self.controller.refresh()
        build = self.retention.begin()
        self.plan(build)
        self.capacity["a"] = 200
        self.controller.refresh()
        job = self.controller.reserve_rotation()
        self.plan(build)
        self.clock.return_value = job["drain_until"]
        with self.assertRaisesRegex(Conflict, "fenced"):
            self.plan(build)

    def test_a_single_old_plan_does_not_stall_a_busy_writer(self):
        self.controller.refresh()
        self.plan(self.retention.begin())
        self.clock.return_value += 29
        for _ in range(2):
            self.plan(self.retention.begin())
        self.clock.return_value += 1
        self.controller.refresh()
        self.assertIsNone(self.controller.reserve_rotation())

    def test_legacy_preparations_adopt_a_deadline_and_respect_write_limit(self):
        capacity = self.config.pop("capacity")
        self.retention = Retention(self.config, self.database)
        prepared = [self.retention.begin() for _ in range(5)]
        self.config["capacity"] = capacity
        self.open()
        self.controller.refresh()
        for build in prepared[:3]:
            self.plan(build)
        with self.assertRaises(CapacityUnavailable):
            self.plan(prepared[3])
        self.publish(prepared[0])
        self.clock.return_value += 60
        self.controller.refresh()
        with self.assertRaisesRegex(CapacityUnavailable, "reservation expired"):
            self.plan(prepared[4])
        self.plan(self.retention.begin())

    def test_stalled_build_limit_rotates_even_below_watermark(self):
        self.config["capacity"]["rotation_interval_seconds"] = 0
        self.open()
        self.controller.refresh()
        for _ in range(3):
            self.plan(self.retention.begin())
        self.controller.cycle()
        self.assertIsNone(self.retention.status()["maintenance"]["slot"])
        self.clock.return_value += 30
        self.controller.refresh()
        self.controller.cycle()
        self.assertEqual(self.retention.begin()["profile"], "harbor-ephemeral-b")
        self.assertFalse(self.retention.status()["maintenance"]["pressure"])
        self.assertEqual(self.calls, [])
        self.clock.return_value += 30
        registry = self.retention.registries["a"]
        with patch.object(registry, "resolve", return_value=None):
            self.controller.cycle()
        self.assertEqual(self.calls.count(("gc", "a")), 1)
        self.assertEqual(self.retention.status()["publications"][0]["state"], "abandoned")

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

    def test_blocked_standby_gc_falls_back_to_active_after_restart(self):
        self.controller.refresh()
        live = self.retention.begin()
        self.plan(live)
        reference, _ = self.publish(live)
        hold = self.retention.acquire(reference, "current-reader")["id"]
        pending = self.retention.begin()
        self.plan(pending)
        self.capacity = {"a": 100, "b": 100}
        self.clock.return_value += 300
        self.controller.refresh()

        def gc_standby_without_space(action, slot):
            result = self.driver(action, slot)
            if action == "gc" and slot == "b":
                self.capacity["b"] = 100
            return result

        self.controller.driver = gc_standby_without_space
        self.controller.cycle()
        self.assertEqual(self.retention.status()["maintenance"]["last_blocked_gc_slot"], "b")
        self.open()
        self.controller.refresh()
        self.controller.cycle()
        job = self.retention.status()["maintenance"]
        self.assertEqual(job["slot"], "a")
        self.assertNotIn("active", {slot["state"] for slot in self.retention.status()["slots"]})
        with self.assertRaisesRegex(CapacityUnavailable, "paused"):
            self.retention.begin()
        self.assertNotIn(("fence", "a"), self.calls)

        # A previously admitted build may still finish within the drain window.
        self.plan(pending)
        self.publish(pending)
        self.open()
        self.controller.refresh()
        self.controller.cycle()
        self.assertEqual(
            self.calls, [(action, slot) for slot in ("b", "a") for action in ("fence", "gc", "restore")]
        )
        self.assertEqual(self.retention.begin()["profile"], "harbor-ephemeral-a")
        with self.retention.database_session() as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM holds WHERE id=?", (hold,)).fetchone())

    def test_paused_pair_alternates_with_cooldown_and_requires_headroom(self):
        self.capacity = {"a": 100, "b": 100}
        self.clock.return_value += 300
        self.controller.refresh()

        def gc_without_usable_space(action, slot):
            result = self.driver(action, slot)
            if action == "gc":
                self.capacity[slot] = 300
            return result

        self.controller.driver = gc_without_usable_space
        self.controller.cycle()
        self.controller.cycle()
        self.controller.cycle()
        self.assertEqual([slot for action, slot in self.calls if action == "gc"], ["b", "a"])
        with self.assertRaisesRegex(CapacityUnavailable, "paused"):
            self.retention.begin()
        self.clock.return_value += 300
        self.controller.refresh()
        self.controller.cycle()
        self.clock.return_value += 300
        self.controller.refresh()
        self.controller.cycle()
        self.assertEqual([slot for action, slot in self.calls if action == "gc"], ["b", "a", "b", "a"])

        self.capacity["b"] = 350
        self.controller.refresh()
        self.controller.cycle()
        self.assertEqual(self.retention.begin()["profile"], "harbor-ephemeral-b")

    def test_abandoned_gc_requires_a_scoped_receipt_and_survives_restart(self):
        self.controller.refresh()
        cleaned, deferred, completed = [self.retention.begin() for _ in range(3)]
        for build in (cleaned, deferred, completed):
            self.plan(build)
        cleaned_repo, deferred_repo, completed_repo = (
            self.retention.registries["a"].repository(self.repository(build))
            for build in (cleaned, deferred, completed)
        )
        completed_reference = self.repository(completed) + "@sha256:" + "b" * 64
        self.capacity["a"] = 100
        self.controller.refresh()
        job = self.controller.reserve_rotation()
        self.assertEqual(job["slot"], "a")
        self.clock.return_value = job["drain_until"]

        def driver_with_reply(reply):
            def run(action, slot, *, payload=None):
                if action != "gc":
                    return self.driver(action, slot)
                self.assertFalse(self.writable[slot])
                self.assertEqual(set(payload["abandoned_repositories"]), {cleaned_repo, deferred_repo})
                return json.dumps({"cleaned_repositories": reply})

            return run

        def resolve(tag):
            return completed_reference if tag.endswith(completed["tag"]) else None

        self.controller.driver = driver_with_reply([completed_repo])
        with (
            patch.object(self.retention.registries["a"], "resolve", side_effect=resolve),
            self.assertRaisesRegex(ValueError, "did not confirm"),
        ):
            self.controller.cycle()
        self.assertEqual(self.retention.status()["abandoned_repositories_pending_gc"], [{"slot": "a", "count": 2}])
        self.assertFalse(self.retention.status()["maintenance"]["gc_complete"])
        self.assertNotIn(("restore", "a"), self.calls)

        self.open()
        self.controller.driver = driver_with_reply([cleaned_repo])
        self.controller.cycle()
        self.assertEqual(self.retention.status()["abandoned_repositories_pending_gc"], [{"slot": "a", "count": 1}])
        self.clock.return_value += self.retention.terminal_receipt_seconds
        self.assertEqual(self.retention.prune_terminal(), 1)
        with self.retention.database_session() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM publications WHERE id=?", (cleaned["id"],)).fetchone())
            self.assertEqual(self.retention.publication(db, deferred["id"])["state"], "abandoned")
            self.assertEqual(self.retention.publication(db, completed["id"])["reference"], completed_reference)
