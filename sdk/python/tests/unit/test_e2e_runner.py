"""Checks for the functional E2E runner's Redis cleanup audit."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.e2e.run import (
    CASES,
    _campaign_elapsed_seconds,
    _case_environment,
    _case_status_with_skips,
    _compare_redis_inventory,
    main,
)


class RedisAuditTest(unittest.TestCase):
    def test_case_environment_prefers_absolute_checkout_sdk(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sdk" / "python"
            with patch.dict("os.environ", {"PYTHONPATH": "relative-sdk"}, clear=True):
                environment = _case_environment(root, (("CASE_VALUE", "enabled"),))

        self.assertEqual(
            environment["PYTHONPATH"].split(os.pathsep),
            [str(root.resolve()), "relative-sdk"],
        )
        self.assertEqual(environment["CASE_VALUE"], "enabled")

    def test_checkpoint_fault_matrix_is_separate_from_standalone(self):
        cases = {case.name: case for case in CASES}
        for name, method in (
            (
                "fault-checkpoint-shared",
                "test_shared_checkpoint_restores_same_id_on_healthy_worker",
            ),
            (
                "fault-checkpoint-local",
                "test_local_only_checkpoint_fails_without_cold_start",
            ),
        ):
            with self.subTest(name=name):
                case = cases[name]
                self.assertEqual(case.level, "full")
                self.assertEqual(case.command[-1].split(".")[-1], method)
        self.assertNotIn("fault-checkpoint-failover", cases)

    def test_partially_skipped_group_is_not_reported_as_fully_passed(self):
        self.assertEqual(_case_status_with_skips("passed", 2, 1), "partial")
        self.assertEqual(_case_status_with_skips("passed", 2, 2), "skipped")
        self.assertEqual(_case_status_with_skips("passed", 2, 0), "passed")
        self.assertEqual(_case_status_with_skips("failed", 2, 1), "failed")

    def test_campaign_uses_run_wall_time_when_available(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "previous"
            run.mkdir()
            (run / "summary.json").write_text(
                json.dumps(
                    {
                        "run_elapsed_seconds": 7.5,
                        "results": [{"elapsed_seconds": 4.0}],
                    }
                )
            )
            self.assertEqual(_campaign_elapsed_seconds(Path(directory)), 7.5)

    def test_campaign_counts_pressure_and_named_profile_summaries(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "pressure"
            run.mkdir()
            (run / "summary.json").write_text(json.dumps({"duration_seconds": 7200.25}))
            profile = Path(directory) / "profiles"
            profile.mkdir()
            (profile / "mixed-summary.json").write_text(
                json.dumps({"duration_seconds": 60.5})
            )
            self.assertEqual(_campaign_elapsed_seconds(Path(directory)), 7260.75)

    def test_preexisting_reservations_do_not_fail_a_new_run(self):
        before = {"held_ids": ["old"], "failed_held_ids": ["old"]}
        after = {"held_ids": ["old"], "failed_held_ids": ["old"]}

        self.assertEqual(
            _compare_redis_inventory(before, after),
            {
                "status": "no-new-held",
                "cluster_clean": False,
                "preexisting_held": 1,
                "new_held_ids": [],
                "released_held_ids": [],
                "new_failed_held_ids": [],
            },
        )

    def test_new_failed_reservation_fails_cleanup_audit(self):
        before = {"held_ids": ["old"], "failed_held_ids": ["old"]}
        after = {
            "held_ids": ["new", "old"],
            "failed_held_ids": ["new", "old"],
        }

        self.assertEqual(
            _compare_redis_inventory(before, after),
            {
                "status": "new-held",
                "cluster_clean": False,
                "preexisting_held": 1,
                "new_held_ids": ["new"],
                "released_held_ids": [],
                "new_failed_held_ids": ["new"],
            },
        )

    def test_running_reservation_left_by_run_is_also_reported(self):
        before = {"held_ids": [], "failed_held_ids": []}
        after = {"held_ids": ["still-running"], "failed_held_ids": []}

        audit = _compare_redis_inventory(before, after)

        self.assertEqual(audit["status"], "new-held")
        self.assertEqual(audit["new_held_ids"], ["still-running"])
        self.assertEqual(audit["new_failed_held_ids"], [])

    def test_existing_reservation_becoming_failed_fails_cleanup_audit(self):
        before = {"held_ids": ["old"], "failed_held_ids": []}
        after = {"held_ids": ["old"], "failed_held_ids": ["old"]}

        audit = _compare_redis_inventory(before, after)

        self.assertEqual(audit["status"], "new-failed-held")
        self.assertEqual(audit["new_held_ids"], [])
        self.assertEqual(audit["new_failed_held_ids"], ["old"])

    def test_no_reservations_is_cluster_clean(self):
        inventory = {"held_ids": [], "failed_held_ids": []}

        audit = _compare_redis_inventory(inventory, inventory)

        self.assertEqual(audit["status"], "no-new-held")
        self.assertTrue(audit["cluster_clean"])

    def test_formal_gate_rejects_dirty_redis_before_running_cases(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            inventory = {"held_ids": ["old"], "failed_held_ids": ["old"]}
            with (
                patch.dict(
                    "os.environ",
                    {
                        "AKERNEL_RUN_INTEGRATION": "1",
                        "AKERNEL_REDIS_HOST": "127.0.0.1",
                    },
                    clear=True,
                ),
                patch(
                    "sys.argv",
                    [
                        "run.py",
                        "--level",
                        "l0",
                        "--output",
                        str(output),
                        "--require-clean-redis",
                    ],
                ),
                patch("tests.e2e.run._record_redis_inventory", return_value=inventory),
                patch("tests.e2e.run.subprocess.run") as subprocess_run,
            ):
                self.assertEqual(main(), 1)

            subprocess_run.assert_not_called()
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["results"], [])
            self.assertEqual(summary["redis_audit"]["status"], "dirty-before")
            self.assertEqual(summary["redis_audit"]["held_count"], 1)
            self.assertGreaterEqual(summary["run_elapsed_seconds"], 0)

    def test_case_outside_selected_level_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.dict("os.environ", {"AKERNEL_RUN_INTEGRATION": "1"}, clear=True),
                patch(
                    "sys.argv",
                    [
                        "run.py",
                        "--level",
                        "standalone",
                        "--case",
                        "port-forward",
                        "--output",
                        str(Path(directory) / "run"),
                    ],
                ),
                patch("tests.e2e.run.subprocess.run") as subprocess_run,
            ):
                with self.assertRaises(SystemExit) as error:
                    main()

            self.assertEqual(error.exception.code, 2)
            subprocess_run.assert_not_called()

    def test_campaign_budget_counts_prior_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory)
            previous = campaign / "previous"
            previous.mkdir()
            (previous / "summary.json").write_text(
                json.dumps({"results": [{"elapsed_seconds": 4.0}]})
            )
            output = campaign / "next"
            with (
                patch.dict("os.environ", {"AKERNEL_RUN_INTEGRATION": "1"}, clear=True),
                patch(
                    "sys.argv",
                    [
                        "run.py",
                        "--level",
                        "l0",
                        "--output",
                        str(output),
                        "--campaign-root",
                        str(campaign),
                        "--budget-seconds",
                        "3",
                    ],
                ),
                patch("tests.e2e.run.subprocess.run") as subprocess_run,
            ):
                self.assertEqual(main(), 1)

            subprocess_run.assert_not_called()
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["campaign_spent_before_seconds"], 4.0)
            self.assertEqual(summary["results"][0]["status"], "budget-exhausted")


if __name__ == "__main__":
    unittest.main()
