"""Disposable image retention state and Registry replay regressions."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from unittest.mock import patch

from retention import Conflict, Retention


class RetentionTests(TestCase):
    def setUp(self):
        data = TemporaryDirectory()
        self.addCleanup(data.cleanup)
        root = Path(data.name)
        token = root / "token"
        token.write_text("fixture-token")
        self.database = root / "retention.sqlite"
        self.config = {
            "adx_origin": "https://api.example.test",
            "runner_token_file": str(token),
            "adx_token_file": str(token),
            "initial_slot": "a",
            "default_ttl_seconds": 30,
            "max_ttl_seconds": 120,
            "auto_delete": True,
            "registries": {
                slot: {
                    "origin": f"https://registry-{slot}.example.test",
                    "profile": f"harbor-ephemeral-{slot}",
                }
                for slot in ("a", "b")
            },
        }
        self.retention = Retention(self.config, self.database)
        self.clock = patch("retention.time.time", return_value=1000.0).start()
        self.addCleanup(patch.stopall)

    def publication(self, ttl=None):
        build = self.retention.begin(ttl)
        registry = self.retention.registries[build["profile"][-1]]
        repository = registry.authority + "/akernel-ephemeral/" + "a" * 32 + "/service-0"
        reference = repository + "@sha256:" + "b" * 64
        tag = repository + ":" + build["tag"]
        self.retention.plan(build["id"], tag)
        with patch.object(registry, "resolve", return_value=reference):
            outcome = self.retention.commit(build["id"], reference)
        return build, reference, outcome["expires_at"]

    def test_ttl_policy_durable_and_idempotent(self):
        build, reference, expires = self.publication()
        self.assertEqual(expires, 1030)
        self.clock.return_value = 1200
        reopened = Retention(self.config, self.database)
        self.assertEqual(reopened.commit(build["id"], reference)["expires_at"], expires)
        self.retention = reopened
        self.retention.switch("b")
        self.assertEqual(self.publication(60)[2], 1260)
        for invalid in (0, -1, True, 1.5, "60", 121):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                reopened.begin(invalid)

    def test_live_and_uncertain_sandbox_protect_expired_image(self):
        _, reference, expires = self.publication()
        hold = self.retention.acquire(reference, "task-1")["id"]
        with patch.object(self.retention, "observe", return_value={"image": reference}):
            self.retention.bind(hold, "default-task-1")
        self.clock.return_value = expires
        with self.assertRaises(Conflict):
            self.retention.acquire(reference, "task-2")
        with patch.object(self.retention, "observe", side_effect=OSError):
            self.assertEqual(self.retention.sweep(apply=False), [])
        self.clock.return_value += 61
        with patch.object(self.retention, "observe", return_value={"image": reference}):
            self.assertEqual(self.retention.sweep(apply=False), [])
        self.clock.return_value += 61
        with patch.object(self.retention, "observe", return_value=None):
            self.assertEqual(self.retention.sweep(apply=False), [])
            self.assertEqual(self.retention.status()["awaiting_stop_confirmation"][0]["id"], hold)
            self.retention.finish(hold, "default-task-1")
            self.assertEqual(len(self.retention.sweep(apply=False)), 1)

    def test_lost_create_response_is_reconciled_without_guessing(self):
        _, reference, expires = self.publication()
        hold = self.retention.acquire(reference, "task-1")["id"]
        self.clock.return_value = expires
        with patch.object(self.retention, "observe", return_value=None):
            self.assertEqual(self.retention.sweep(apply=False), [])
        self.assertEqual(self.retention.status()["unconfirmed_holds"][0]["id"], hold)
        self.clock.return_value += 61
        with patch.object(self.retention, "observe", return_value={"image": reference}):
            self.assertEqual(self.retention.sweep(apply=False), [])
        self.assertEqual(self.retention.status()["unconfirmed_holds"], [])
        self.clock.return_value += 61
        with patch.object(self.retention, "observe", return_value=None):
            self.assertEqual(self.retention.sweep(apply=False), [])
            self.retention.release_stopped(hold)
            self.assertEqual(len(self.retention.sweep(apply=False)), 1)

    def test_publication_repository_is_exclusive(self):
        _, reference, _ = self.publication()
        duplicate = self.retention.begin()
        tag = reference.split("@", 1)[0] + ":" + duplicate["tag"]
        with self.assertRaises(Conflict):
            self.retention.plan(duplicate["id"], tag)

    def test_absent_registry_tag_cannot_commit_without_a_digest(self):
        build = self.retention.begin()
        repository = "registry-a.example.test/akernel-ephemeral/" + "a" * 32 + "/service-0"
        self.retention.plan(build["id"], repository + ":" + build["tag"])
        registry = self.retention.registries["a"]
        with (
            patch.object(registry, "resolve", return_value=None),
            self.assertRaises(ValueError),
        ):
            self.retention.commit(build["id"], None)

    def test_binding_checks_the_authoritative_instance_image(self):
        _, reference, _ = self.publication()
        hold = self.retention.acquire(reference, "task-1")["id"]
        with (
            patch.object(self.retention, "observe", return_value={"image": "legacy"}),
            self.assertRaises(Conflict),
        ):
            self.retention.bind(hold, "default-task-1")
        self.assertEqual(self.retention.status()["unconfirmed_holds"][0]["id"], hold)

    def test_retry_uses_persisted_manifest_worklist(self):
        build, reference, expires = self.publication()
        a = self.retention.registries["a"]
        self.clock.return_value = expires
        with (
            patch.object(a, "worklist", return_value=["sha256:" + "b" * 64]) as plan,
            patch.object(a, "delete", side_effect=[OSError("disconnect"), None]) as delete,
        ):
            self.retention.sweep(apply=True)
            self.clock.return_value += 61
            self.retention.sweep(apply=True)
        self.assertEqual(plan.call_count, 1)
        self.assertEqual(delete.call_count, 2)
        with self.retention.database_session() as db:
            self.assertEqual(self.retention.publication(db, build["id"])["state"], "deleted")

    def test_terminal_receipts_expire_in_batches_without_removing_live_state(self):
        self.config["terminal_receipt_seconds"] = 60
        self.retention = Retention(self.config, self.database)
        for _ in range(257):
            self.retention.cancel_build(self.retention.begin()["id"])
        deleted, _, expires = self.publication()
        self.clock.return_value = expires
        with (
            patch.object(self.retention.registries["a"], "worklist", return_value=["sha256:" + "b" * 64]),
            patch.object(self.retention.registries["a"], "delete"),
        ):
            self.retention.sweep(apply=True)
        self.retention.switch("b")
        live, reference, _ = self.publication()
        hold_id = self.retention.acquire(reference, "reader")["id"]
        pending = self.retention.begin()
        repo = self.retention.registries["b"].authority + "/akernel-ephemeral/" + "c" * 32 + "/service-0"
        self.retention.plan(pending["id"], repo + ":" + pending["tag"])

        self.clock.return_value = 1059
        self.assertEqual(self.retention.prune_terminal(), 0)
        self.clock.return_value = 1060
        self.assertEqual(self.retention.prune_terminal(), 256)
        self.assertEqual(self.retention.prune_terminal(), 1)
        self.clock.return_value = expires + 60
        self.assertEqual(self.retention.prune_terminal(), 1)
        with self.retention.database_session() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM publications WHERE id=?", (deleted["id"],)).fetchone())
            self.assertEqual(self.retention.publication(db, pending["id"])["state"], "planned")
            self.assertEqual(self.retention.publication(db, live["id"])["state"], "published")
            self.assertIsNotNone(db.execute("SELECT 1 FROM holds WHERE id=?", (hold_id,)).fetchone())

    def test_migration_starts_terminal_grace_and_preserves_unconfirmed_abandonment(self):
        cancelled = self.retention.begin()
        self.retention.cancel_build(cancelled["id"])
        pending = self.retention.begin()
        repo = self.retention.registries["a"].authority + "/akernel-ephemeral/" + "a" * 32 + "/service-0"
        self.retention.plan(pending["id"], repo + ":" + pending["tag"])
        self.retention.switch("b")
        with patch.object(self.retention.registries["a"], "resolve", return_value=None):
            self.retention.seal("a")
        with self.retention.database_session(write=True) as db:
            db.execute("DROP INDEX publication_terminal")
            db.execute("DROP INDEX publication_abandoned_gc")
            db.execute("ALTER TABLE publications DROP COLUMN terminal_at")
        self.config["terminal_receipt_seconds"] = 10
        self.clock.return_value = 5000
        self.retention = Retention(self.config, self.database)
        self.assertEqual(self.retention.prune_terminal(), 0)
        self.clock.return_value = 5010
        self.retention = Retention(self.config, self.database)
        self.assertEqual(self.retention.prune_terminal(), 1)
        with self.retention.database_session() as db:
            self.assertEqual(self.retention.publication(db, pending["id"])["state"], "abandoned")

    def test_switch_drains_old_publications_without_rebinding(self):
        stale = self.retention.begin()
        self.retention.switch("b")
        self.assertEqual(self.retention.begin()["profile"], "harbor-ephemeral-b")
        with self.assertRaises(Conflict):
            self.retention.plan(
                stale["id"],
                "registry-a.example.test/akernel-ephemeral/"
                + "f" * 32
                + "/service-0:"
                + stale["tag"],
            )
        with self.assertRaises(Conflict):
            self.retention.switch("a")
        self.retention.seal("a")
        with self.assertRaises(Conflict):
            self.retention.switch("a")
        self.retention.ready("a")
        self.retention.switch("a")
        self.assertEqual(self.retention.begin()["profile"], "harbor-ephemeral-a")

    def test_only_authoritative_not_found_releases_a_hold(self):
        identity = "default-task"
        with patch("retention.request", return_value=(404, {}, b"{}")):
            with self.assertRaises(ValueError):
                self.retention.observe(identity)
        with patch(
            "retention.request",
            return_value=(
                404,
                {},
                b'{"error":{"code":"NOT_FOUND","environmentId":"default-task"}}',
            ),
        ):
            self.assertIsNone(self.retention.observe(identity))

    def test_slot_cannot_be_rebound_to_different_storage_origin(self):
        self.config["registries"]["a"]["origin"] = "https://elsewhere.example.test"
        with self.assertRaises(ValueError):
            Retention(self.config, self.database)


if __name__ == "__main__":
    main()
