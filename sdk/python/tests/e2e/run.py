"""Run independent AKernel SDK E2E groups with a strict aggregate time budget.

Each group writes its own log and runs even when a preceding group fails.
Failure analysis and reruns can therefore start from a complete first pass.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


@dataclasses.dataclass(frozen=True)
class Case:
    name: str
    level: str
    command: tuple[str, ...]
    timeout_seconds: int
    env: tuple[tuple[str, str], ...] = ()


CASES = (
    Case(
        "sdk-contracts",
        "l0",
        ("-m", "unittest", "-v", "tests.e2e.standalone.test_sdk_contracts"),
        300,
    ),
    Case(
        "lifecycle",
        "standalone",
        ("-m", "unittest", "-v", "tests.e2e.standalone.test_lifecycle_features"),
        360,
    ),
    Case(
        "auth",
        "standalone",
        ("-m", "unittest", "-v", "tests.e2e.standalone.test_auth_contract"),
        120,
    ),
    Case(
        "cli-contract",
        "standalone",
        ("-m", "unittest", "-v", "tests.e2e.standalone.test_cli_contract"),
        180,
    ),
    Case(
        "io",
        "standalone",
        ("-m", "unittest", "-v", "tests.e2e.standalone.test_io_features"),
        300,
    ),
    Case(
        "pty-close",
        "standalone",
        ("-m", "unittest", "-v", "tests.e2e.standalone.test_pty_lifecycle"),
        180,
    ),
    Case(
        "network-url",
        "standalone",
        (
            "-m",
            "unittest",
            "-v",
            "tests.e2e.standalone.test_network_features.NetworkFeaturesIntegrationTest.test_declared_port_url_and_undeclared_port_rejection",
            "tests.e2e.standalone.test_network_features.NetworkFeaturesIntegrationTest.test_internal_port_url_produces_an_edge_address",
        ),
        180,
    ),
    Case(
        "network-tunnel",
        "standalone",
        (
            "-m",
            "unittest",
            "-v",
            "tests.e2e.standalone.test_network_features.NetworkFeaturesIntegrationTest.test_reverse_tunnel_reaches_sdk_host_service",
        ),
        180,
    ),
    Case(
        "network-policy",
        "standalone",
        (
            "-m",
            "unittest",
            "-v",
            "tests.e2e.standalone.test_network_features.NetworkFeaturesIntegrationTest.test_dynamic_block_and_clear_preserve_control_operations",
        ),
        180,
    ),
    Case(
        "runtime-integration",
        "standalone",
        ("-m", "unittest", "-v", "tests.integration.test_sandbox"),
        900,
    ),
    Case("storage-quota", "standalone", ("examples/storage_sandbox.py",), 240),
    Case("network-policy-matrix", "standalone", ("examples/network_policy.py",), 360),
    Case(
        "node-placement",
        "multi-vm",
        ("-m", "unittest", "-v", "tests.e2e.multi_vm.test_node_placement"),
        360,
    ),
    Case(
        "redis-lifecycle",
        "full",
        ("-m", "unittest", "-v", "tests.e2e.full.test_redis_lifecycle"),
        300,
    ),
    Case(
        "placement-affinity",
        "full",
        ("-m", "unittest", "-v", "tests.e2e.full.test_placement_affinity"),
        360,
    ),
    Case(
        "admission",
        "full",
        ("-m", "unittest", "-v", "tests.e2e.full.test_admission_contract"),
        180,
    ),
    Case(
        "concurrent-claim",
        "full",
        ("-m", "unittest", "-v", "tests.e2e.full.test_concurrent_claim"),
        240,
    ),
    Case(
        "unknown-create-result",
        "full",
        ("-m", "unittest", "-v", "tests.e2e.full.test_unknown_create_result"),
        240,
    ),
    Case(
        "create-transport-disconnect",
        "full",
        ("-m", "unittest", "-v", "tests.e2e.full.test_create_transport_disconnect"),
        240,
    ),
    Case("custom-image", "full", ("examples/custom_image.py",), 300),
    Case("dockerfile", "full", ("examples/dockerfile_launch.py",), 900),
    Case(
        "port-forward",
        "full",
        ("-m", "unittest", "-v", "tests.e2e.full.test_port_forward"),
        300,
    ),
    Case(
        "storage-sources",
        "full",
        ("-m", "unittest", "-v", "tests.e2e.full.test_storage_sources"),
        900,
    ),
    Case(
        "tenant-isolation",
        "full",
        ("-m", "unittest", "-v", "tests.e2e.full.test_tenant_isolation"),
        240,
    ),
    Case(
        "key-lifecycle",
        "full",
        ("-m", "unittest", "-v", "tests.e2e.full.test_key_lifecycle"),
        180,
    ),
    Case(
        "scheduler-admin",
        "full",
        (
            "-m",
            "unittest",
            "-v",
            "tests.e2e.full.test_scheduler_admin.SchedulerAdminIntegrationTest.test_tenant_cannot_read_queue_or_change_node_scheduling",
        ),
        120,
    ),
    Case(
        "scheduler-maintenance",
        "full",
        (
            "-m",
            "unittest",
            "-v",
            "tests.e2e.full.test_scheduler_admin.SchedulerAdminIntegrationTest.test_administrator_pause_and_resume_preserves_running_sandbox",
        ),
        180,
    ),
    Case(
        "fault-process-restart",
        "full",
        ("-m", "unittest", "-v", "tests.e2e.fault.test_process_restart"),
        600,
    ),
    Case(
        "fault-node-loss",
        "full",
        ("-m", "unittest", "-v", "tests.e2e.fault.test_node_loss"),
        900,
    ),
    Case(
        "fault-checkpoint-shared",
        "full",
        (
            "-m",
            "unittest",
            "-v",
            "tests.e2e.fault.test_checkpoint_failover.CheckpointFailoverIntegrationTest.test_shared_checkpoint_restores_same_id_on_healthy_worker",
        ),
        450,
    ),
    Case(
        "fault-checkpoint-local",
        "full",
        (
            "-m",
            "unittest",
            "-v",
            "tests.e2e.fault.test_checkpoint_failover.CheckpointFailoverIntegrationTest.test_local_only_checkpoint_fails_without_cold_start",
        ),
        450,
    ),
)
LEVELS = ("l0", "standalone", "multi-vm", "full")


def _summary_line(log: str) -> tuple[int | None, int]:
    match = re.search(r"Ran (\d+) tests? in", log)
    total = int(match.group(1)) if match else None
    skipped = re.search(r"OK \(skipped=(\d+)\)", log)
    if skipped is None:
        skipped = re.search(r"FAILED \([^)]*skipped=(\d+)", log)
    return total, int(skipped.group(1)) if skipped else 0


def _case_status_with_skips(status: str, count: int | None, skipped: int) -> str:
    if status != "passed" or count is None or not skipped:
        return status
    return "skipped" if count == skipped else "partial"


def _capture_redis_inventory() -> dict[str, object]:
    """Read persisted ownership without changing Redis or printing credentials."""
    from tests.e2e.full.test_redis_lifecycle import _Redis

    client = _Redis()
    try:
        namespace = os.environ.get("AKERNEL_REDIS_NAMESPACE", "akernel")
        fields = client.call("HGETALL", f"adx:{{{namespace}}}:control:v1")
        states: dict[str, int] = {}
        held_ids: list[str] = []
        failed_held_ids: list[str] = []
        for field, value in zip(fields[0::2], fields[1::2], strict=True):
            key = field.decode()
            if not key.startswith("environment:"):
                continue
            record = json.loads(value)
            result = record.get("result") or {}
            state = result.get("state", "Unknown")
            states[state] = states.get(state, 0) + 1
            if result.get("resources_held"):
                environment_id = key.removeprefix("environment:")
                held_ids.append(environment_id)
                if state == "Failed":
                    failed_held_ids.append(environment_id)
        return {
            "states": states,
            "held_ids": sorted(held_ids),
            "failed_held_ids": sorted(failed_held_ids),
        }
    finally:
        client.close()


def _compare_redis_inventory(
    before: dict[str, object], after: dict[str, object]
) -> dict[str, object]:
    """Report reservations newly left behind by this E2E campaign."""
    old_held = set(before["held_ids"])
    new_held = set(after["held_ids"])
    newly_failed_held = set(after["failed_held_ids"]) - set(before["failed_held_ids"])
    return {
        "status": (
            "new-held"
            if new_held - old_held
            else "new-failed-held"
            if newly_failed_held
            else "no-new-held"
        ),
        "cluster_clean": not new_held,
        "preexisting_held": len(old_held),
        "new_held_ids": sorted(new_held - old_held),
        "released_held_ids": sorted(old_held - new_held),
        "new_failed_held_ids": sorted(newly_failed_held),
    }


def _record_redis_inventory(path: Path) -> dict[str, object]:
    try:
        inventory = _capture_redis_inventory()
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        AttributeError,
    ) as error:
        inventory = {"error": f"{type(error).__name__}: {error}"}
    path.write_text(json.dumps(inventory, indent=2, ensure_ascii=False) + "\n")
    return inventory


def _campaign_elapsed_seconds(root: Path) -> float:
    """Count complete run time, including setup and audits, across a campaign."""
    elapsed = 0.0
    for path in root.glob("*/*summary.json"):
        summary = json.loads(path.read_text(encoding="utf-8"))
        if "run_elapsed_seconds" in summary:
            elapsed += float(summary["run_elapsed_seconds"])
        elif "duration_seconds" in summary:
            elapsed += float(summary["duration_seconds"])
        else:
            # Results written before this field existed only retained group time.
            elapsed += sum(
                float(result.get("elapsed_seconds", 0))
                for result in summary.get("results", [])
            )
    return elapsed


def _case_environment(
    root: Path, overrides: tuple[tuple[str, str], ...]
) -> dict[str, str]:
    """Build a child environment that always imports this checkout's SDK first."""
    environment = os.environ.copy()
    environment.update(overrides)
    python_path = [str(root.resolve())]
    if inherited := environment.get("PYTHONPATH"):
        python_path.append(inherited)
    environment["PYTHONPATH"] = os.pathsep.join(python_path)
    return environment


def main() -> int:
    run_started = time.monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--level", choices=LEVELS, default="full")
    parser.add_argument(
        "--case",
        action="append",
        choices=[case.name for case in CASES],
        help="run only the named group; repeat for multiple groups",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget-seconds", type=int, default=10800)
    parser.add_argument(
        "--campaign-root",
        type=Path,
        help="count prior run durations under this root against the same budget",
    )
    parser.add_argument(
        "--require-clean-redis",
        action="store_true",
        help="formal gate: require zero held reservations before and after tests",
    )
    args = parser.parse_args()
    if not 0 < args.budget_seconds <= 10800:
        parser.error("budget-seconds must be between 1 and 10800")
    if os.environ.get("AKERNEL_RUN_INTEGRATION") != "1":
        parser.error("set AKERNEL_RUN_INTEGRATION=1 for a real deployment run")
    if args.case and not any(
        case.name in args.case and LEVELS.index(case.level) <= LEVELS.index(args.level)
        for case in CASES
    ):
        parser.error("selected cases are outside the requested level")

    campaign_spent = 0.0
    if args.campaign_root is not None:
        root = args.campaign_root.resolve()
        output = args.output.resolve()
        if output.parent != root:
            parser.error("--output must be a direct child of --campaign-root")
        if (output / "summary.json").exists():
            parser.error("campaign runs need unique output directories")
        try:
            campaign_spent = _campaign_elapsed_seconds(root)
        except (OSError, ValueError, TypeError) as error:
            parser.error(f"cannot read campaign results: {error}")
    args.output.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[2]
    deadline = run_started + max(0.0, args.budget_seconds - campaign_spent)
    results = []
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    def write_summary(redis_audit: dict[str, object] | None = None) -> None:
        summary = {
            "started_at": started_at,
            "level": args.level,
            "campaign_spent_before_seconds": round(campaign_spent, 3),
            "run_elapsed_seconds": round(time.monotonic() - run_started, 3),
            "results": results,
        }
        if redis_audit is not None:
            summary["redis_audit"] = redis_audit
        (args.output / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    redis_enabled = bool(os.environ.get("AKERNEL_REDIS_HOST"))
    if args.require_clean_redis and not redis_enabled:
        parser.error("--require-clean-redis needs AKERNEL_REDIS_HOST")
    redis_before = (
        _record_redis_inventory(args.output / "redis-before.json")
        if redis_enabled
        else None
    )
    if args.require_clean_redis and (
        "error" in redis_before or redis_before["held_ids"]
    ):
        redis_audit = {
            "status": "dirty-before",
            "reason": "Redis unavailable or held reservations exist before testing",
            "held_count": len(redis_before.get("held_ids", [])),
        }
        write_summary(redis_audit)
        return 1
    for case in CASES:
        if LEVELS.index(case.level) > LEVELS.index(args.level):
            continue
        if args.case and case.name not in args.case:
            continue
        remaining = deadline - time.monotonic()
        if remaining < 1:
            results.append(
                {"name": case.name, "level": case.level, "status": "budget-exhausted"}
            )
            continue
        timeout = min(case.timeout_seconds, remaining)
        log_path = args.output / f"{case.name}.log"
        started = time.monotonic()
        print(f"START {case.level}/{case.name} timeout={timeout:.0f}s", flush=True)
        environment = _case_environment(root, case.env)
        with log_path.open("w", encoding="utf-8") as log:
            try:
                completed = subprocess.run(
                    (sys.executable, *case.command),
                    cwd=root,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=timeout,
                    check=False,
                    env=environment,
                )
                status = "passed" if completed.returncode == 0 else "failed"
                exit_code = completed.returncode
            except subprocess.TimeoutExpired:
                status, exit_code = "timed-out", None
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        count, skipped = _summary_line(log_text)
        status = _case_status_with_skips(status, count, skipped)
        result = {
            "name": case.name,
            "level": case.level,
            "status": status,
            "exit_code": exit_code,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "tests": count,
            "skipped_tests": skipped,
            "log": str(log_path),
        }
        results.append(result)
        print(
            f"END {case.level}/{case.name} {status} {result['elapsed_seconds']}s",
            flush=True,
        )
        write_summary()

    if redis_enabled:
        redis_after = _record_redis_inventory(args.output / "redis-after.json")
        if "error" in redis_before or "error" in redis_after:
            redis_audit = {
                "status": "unavailable",
                "before_error": redis_before.get("error"),
                "after_error": redis_after.get("error"),
            }
        else:
            redis_audit = _compare_redis_inventory(redis_before, redis_after)
    else:
        redis_audit = {"status": "skipped", "reason": "AKERNEL_REDIS_HOST not set"}
    write_summary(redis_audit)
    return int(
        any(r["status"] in {"failed", "timed-out", "budget-exhausted"} for r in results)
        or redis_audit["status"] in {"new-held", "new-failed-held", "unavailable"}
        or (args.require_clean_redis and not redis_audit["cluster_clean"])
    )


if __name__ == "__main__":
    raise SystemExit(main())
