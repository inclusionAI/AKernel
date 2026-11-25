"""Schedule maintenance of independent Distribution volumes."""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from retention import Retention

LOG = logging.getLogger(__name__)
BLOCKED_RETRY_SECONDS = 300
SCHEMA = """
CREATE TABLE IF NOT EXISTS registry_capacity (
    slot TEXT PRIMARY KEY REFERENCES slots(name), storage_id TEXT NOT NULL, sampled_at INTEGER NOT NULL,
    total_bytes INTEGER NOT NULL, available_bytes INTEGER NOT NULL,
    writable INTEGER NOT NULL, blocked INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS registry_maintenance (
    id INTEGER PRIMARY KEY CHECK(id=1), slot TEXT REFERENCES slots(name),
    pressure INTEGER NOT NULL DEFAULT 0, drain_until INTEGER NOT NULL DEFAULT 0,
    gc_complete INTEGER NOT NULL DEFAULT 0, completed_at INTEGER NOT NULL,
    last_blocked_gc_slot TEXT REFERENCES slots(name)
);
"""


class CapacityUnavailable(RuntimeError):
    """The build was rejected before it could write to a Registry."""


@dataclass(frozen=True)
class CapacityPolicy:
    driver: str
    storage_ids: dict[str, str]
    high_watermark_percent: int = 80
    low_watermark_percent: int = 65
    max_in_flight_builds: int = 16
    prepare_seconds: int = 600
    poll_seconds: int = 10
    max_sample_age_seconds: int = 60
    rotation_interval_seconds: int = 21600
    drain_seconds: int = 1800
    driver_timeout_seconds: int = 3600
    lru_idle_seconds: int | None = None

    def validate(self, slots: set[str]):
        if not isinstance(self.driver, str) or not Path(self.driver).is_absolute():
            raise ValueError("Capacity driver must be an absolute executable path")
        if (
            not isinstance(self.storage_ids, dict)
            or set(self.storage_ids) != slots
            or any(not isinstance(value, str) or not value.strip() for value in self.storage_ids.values())
            or len(set(self.storage_ids.values())) != len(slots)
        ):
            raise ValueError("Capacity management requires distinct storage identities for all slots")
        for name, minimum in (
            ("low_watermark_percent", 1), ("high_watermark_percent", 2),
            ("max_in_flight_builds", 1), ("prepare_seconds", 1),
            ("poll_seconds", 1), ("max_sample_age_seconds", 1),
            ("rotation_interval_seconds", 0), ("drain_seconds", 1), ("driver_timeout_seconds", 1),
        ):
            value = getattr(self, name)
            if type(value) is not int or value < minimum:
                raise ValueError(f"Invalid capacity setting {name}")
        if not self.low_watermark_percent < self.high_watermark_percent < 100:
            raise ValueError("Capacity watermarks must satisfy 0 < low < high < 100")
        if self.max_sample_age_seconds <= self.poll_seconds:
            raise ValueError("Capacity samples must be valid for longer than a poll interval")
        if self.lru_idle_seconds is not None and (
            type(self.lru_idle_seconds) is not int or self.lru_idle_seconds < 1
        ):
            raise ValueError("lru_idle_seconds must be a positive integer or null")

    def fresh(self, sample: sqlite3.Row | None, now: int) -> bool:
        return (
            sample is not None and sample["storage_id"] == self.storage_ids.get(sample["slot"])
            and sample["sampled_at"] >= 0
            and 0 <= now - sample["sampled_at"] <= self.max_sample_age_seconds
        )

    def available(self, sample: sqlite3.Row | None, now: int) -> bool:
        return (
            sample is not None and self.fresh(sample, now)
            and bool(sample["writable"]) and not sample["blocked"]
        )

    def blocked(self, sample: sqlite3.Row | None, now: int) -> bool:
        return sample is not None and self.fresh(sample, now) and bool(sample["blocked"])


@contextmanager
def maintenance_lock(database: Path):
    """Serialize operators on the same volume as the SQLite database."""
    import fcntl

    with database.with_name(database.name + ".maintenance.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another Registry maintenance process owns this database") from exc
        yield


class CapacityController:
    def __init__(self, retention: Retention):
        self.retention = retention
        if retention.capacity is None:
            raise ValueError("Configure capacity before starting Registry maintenance")
        self.policy = retention.capacity
        self.probe_locks = {slot: threading.Lock() for slot in retention.registries}

    def driver(self, action: str, slot: str) -> str:
        command = [self.policy.driver, action, slot, self.policy.storage_ids[slot]]
        timeout = min(10, self.policy.max_sample_age_seconds) if action == "measure" else (
            self.policy.driver_timeout_seconds
        )
        try:
            return subprocess.run(
                command, check=True, capture_output=True, text=True, timeout=timeout,
            ).stdout
        except (subprocess.SubprocessError, OSError) as exc:
            raise OSError(f"Registry maintenance driver {action} failed for {slot}") from exc

    def measure(self, slot: str) -> bool:
        # A delayed probe must not overwrite a later observation of the same slot.
        with self.probe_locks[slot]:
            try:
                return self._record_measurement(slot, int(time.time()))
            except (OSError, ValueError):
                with self.retention.database_session(write=True) as db:
                    db.execute("UPDATE registry_capacity SET sampled_at=-1 WHERE slot=?", (slot,))
                raise

    def _record_measurement(self, slot: str, observed_since: int) -> bool:
        sample = json.loads(self.driver("measure", slot))
        if not isinstance(sample, dict) or sample.get("storage_id") != self.policy.storage_ids[slot]:
            raise ValueError(f"Registry storage identity could not be verified for {slot}")
        total = sample.get("total_bytes")
        available = sample.get("available_bytes")
        writable = sample.get("writable")
        if (
            type(total) is not int or type(available) is not int
            or not 0 <= available <= total < 2**63 or total == 0 or type(writable) is not bool
        ):
            raise ValueError(f"Invalid Registry capacity measurement for {slot}")
        used = (total - available) * 100
        with self.retention.database_session(write=True) as db:
            previous = db.execute(
                "SELECT blocked FROM registry_capacity WHERE slot=? AND storage_id=?",
                (slot, sample["storage_id"]),
            ).fetchone()
            blocked = used >= total * self.policy.high_watermark_percent or (
                previous is not None and previous["blocked"]
                and used > total * self.policy.low_watermark_percent
            )
            db.execute(
                """INSERT INTO registry_capacity VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(slot) DO UPDATE SET storage_id=excluded.storage_id, sampled_at=excluded.sampled_at,
                total_bytes=excluded.total_bytes, available_bytes=excluded.available_bytes,
                writable=excluded.writable, blocked=excluded.blocked""",
                (slot, sample["storage_id"], observed_since, total, available, writable, blocked),
            )
        return writable

    def refresh(self):
        for slot in self.retention.registries:
            self.refresh_slot(slot)

    def refresh_slot(self, slot: str):
        try:
            self.measure(slot)
        except (OSError, ValueError, sqlite3.Error) as exc:
            LOG.warning("Registry capacity sample failed for %s (%s)", slot, type(exc).__name__)

    def reserve_rotation(self):
        now = int(time.time())
        with self.retention.database_session(write=True) as db:
            job = db.execute("SELECT * FROM registry_maintenance WHERE id=1").fetchone()
            if job["slot"] is not None:
                return job
            slots = dict(db.execute("SELECT name,state FROM slots ORDER BY name"))
            samples = {row["slot"]: row for row in db.execute("SELECT * FROM registry_capacity")}
            active = next((slot for slot, state in slots.items() if state == "active"), None)
            ready = [slot for slot, state in slots.items() if state == "ready"]
            if active is None:
                for slot in ready:
                    if self.policy.available(samples.get(slot), now):
                        self.retention.switch_slot(db, slot, now)
                        db.execute("UPDATE registry_maintenance SET last_blocked_gc_slot=NULL WHERE id=1")
                        return None
                if now - job["completed_at"] < BLOCKED_RETRY_SECONDS:
                    return None
                # A paused pair tries the other volume first on the next pass.
                ready.sort(key=lambda slot: slot == job["last_blocked_gc_slot"])
                retired = next((slot for slot in ready if self.policy.blocked(samples.get(slot), now)), None)
                if retired is not None:
                    self.retention.retire_slot(db, retired)
                pressure = True
            elif ready and self.policy.fresh(samples.get(active), now):
                retired, pressure = self.choose_rotation(db, job, active, ready[0], samples, now)
            else:
                return None
            if retired is None:
                return None
            db.execute(
                "UPDATE registry_maintenance SET slot=?, pressure=?, drain_until=?, gc_complete=0 WHERE id=1",
                (retired, pressure, now + self.policy.drain_seconds),
            )
            LOG.info("Scheduled Registry GC for %s (pressure=%s)", retired, pressure)
            return db.execute("SELECT * FROM registry_maintenance WHERE id=1").fetchone()

    def choose_rotation(self, db, job, active, standby, samples, now):
        pressure = self.policy.blocked(samples.get(active), now)
        elapsed = now - job["completed_at"]
        periodic = self.policy.rotation_interval_seconds and elapsed >= self.policy.rotation_interval_seconds
        in_flight = db.execute(
            """SELECT COUNT(*) AS count, MAX(planned_at) AS newest
            FROM publications WHERE slot=? AND state='planned'""",
            (active,),
        ).fetchone()
        stalled = (
            in_flight["count"] >= self.policy.max_in_flight_builds
            and in_flight["newest"] is not None
            and in_flight["newest"] + self.policy.drain_seconds <= now
        )
        if not pressure and not periodic and not stalled:
            return None, False
        if self.policy.available(samples.get(standby), now):
            self.retention.switch_slot(db, standby, now)
            return active, pressure
        if (pressure or stalled) and self.policy.blocked(samples.get(standby), now):
            if job["last_blocked_gc_slot"] == standby:
                # The standby was still saturated after GC. Pause admission
                # and drain the former writer before trying its volume.
                self.retention.retire_slot(db, active)
                return active, True
            if elapsed >= BLOCKED_RETRY_SECONDS:
                self.retention.retire_slot(db, standby)
                return standby, True
        return None, False

    def cycle(self):
        if not self.retention.auto_delete:
            return
        job = self.reserve_rotation()
        if job is None:
            return
        slot = job["slot"]
        with self.retention.database_session() as db:
            state = db.execute("SELECT state FROM slots WHERE name=?", (slot,)).fetchone()["state"]
            pending = db.execute(
                "SELECT 1 FROM publications WHERE slot=? AND state='planned' LIMIT 1", (slot,)
            ).fetchone()
        valid_states = {"sealed"} if job["gc_complete"] else {"draining", "sealed"}
        if state not in valid_states:
            raise ValueError("Registry maintenance lost exclusive access to its slot")
        if not job["gc_complete"]:
            if state == "draining" and pending and time.time() < job["drain_until"]:
                return
            if state == "draining":
                try:
                    writable = self.measure(slot)
                except OSError:
                    # The last fence attempt may have stopped the endpoint. The
                    # driver revalidates its volume while bringing up read-only.
                    writable = False
                if writable:
                    self.retention.sweep(apply=True, slot=slot, pressure=bool(job["pressure"]))
            self.driver("fence", slot)
            if self.measure(slot):
                raise ValueError("Registry writers must be fenced before GC")
            self.retention.seal(slot)
            self.driver("gc", slot)
            with self.retention.database_session(write=True) as db:
                db.execute("UPDATE registry_maintenance SET gc_complete=1 WHERE id=1")
        self.driver("restore", slot)
        if not self.measure(slot):
            raise ValueError("Registry is still read-only after maintenance")
        with self.retention.database_session(write=True) as db:
            self.retention.ready_slot(db, slot)
            now = int(time.time())
            sample = db.execute("SELECT * FROM registry_capacity WHERE slot=?", (slot,)).fetchone()
            if self.policy.available(sample, now) and not db.execute(
                "SELECT 1 FROM slots WHERE state='active'"
            ).fetchone():
                self.retention.switch_slot(db, slot, now)
            db.execute(
                """UPDATE registry_maintenance SET slot=NULL, pressure=0,
                drain_until=0, gc_complete=0, completed_at=?, last_blocked_gc_slot=? WHERE id=1""",
                (now, slot if job["pressure"] and sample["blocked"] else None),
            )
        LOG.info("Completed Registry GC for %s", slot)

    def run(self, *, once: bool):
        stopped = threading.Event()

        def sampling(slot):
            # A slow driver probe on a retiring volume must not block the writer's probe.
            while not stopped.wait(self.policy.poll_seconds):
                self.refresh_slot(slot)

        with maintenance_lock(self.retention.database):
            self.refresh()
            samplers = [
                threading.Thread(target=sampling, args=(slot,), daemon=True)
                for slot in self.retention.registries
            ]
            for sampler in samplers:
                sampler.start()
            try:
                while True:
                    try:
                        self.cycle()
                    except (OSError, ValueError, sqlite3.Error) as exc:
                        if once:
                            raise
                        LOG.warning("Registry maintenance will retry (%s)", exc)
                    if once:
                        break
                    stopped.wait(self.policy.poll_seconds)
            finally:
                stopped.set()
                for sampler in samplers:
                    sampler.join()
