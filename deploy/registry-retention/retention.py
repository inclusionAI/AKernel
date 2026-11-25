"""Private AKernel publication and usage service for disposable images."""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import math
import re
import sqlite3
import ssl
import threading
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from capacity import SCHEMA as CAPACITY_SCHEMA
from capacity import CapacityController, CapacityPolicy, CapacityUnavailable, maintenance_lock
from registry import ABANDONED_BATCH_SIZE, DIGEST, Registry, request, trusted_url

LOG = logging.getLogger(__name__)
SCHEMA = """
CREATE TABLE IF NOT EXISTS slots (
    name TEXT PRIMARY KEY, origin TEXT NOT NULL, profile TEXT NOT NULL,
    state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publications (
    id TEXT PRIMARY KEY, slot TEXT NOT NULL REFERENCES slots(name),
    ttl INTEGER NOT NULL, state TEXT NOT NULL, tag TEXT UNIQUE,
    reference TEXT UNIQUE, expires_at INTEGER, worklist TEXT, retry_at INTEGER NOT NULL DEFAULT 0,
    pressure_evictable INTEGER NOT NULL DEFAULT 0, last_used_at INTEGER,
    planned_at INTEGER, prepare_until INTEGER, terminal_at INTEGER
);
CREATE TABLE IF NOT EXISTS holds (
    id TEXT PRIMARY KEY, publication TEXT NOT NULL REFERENCES publications(id),
    instance TEXT NOT NULL, bound INTEGER NOT NULL DEFAULT 0,
    finished INTEGER NOT NULL DEFAULT 0, finished_at INTEGER,
    retry_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS publication_expiry ON publications(state, expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS publication_repository
    ON publications(substr(tag, 1, instr(tag, ':build-') - 1)) WHERE tag IS NOT NULL;
CREATE INDEX IF NOT EXISTS holds_publication ON holds(publication);
"""


class Conflict(ValueError):
    pass


class Retention:
    def __init__(self, config: dict, database: Path):
        self.database = database
        self.registries = {name: Registry(cfg) for name, cfg in config["registries"].items()}
        origins = {registry.authority for registry in self.registries.values()}
        profiles = {registry.profile for registry in self.registries.values()}
        if len(self.registries) != 2 or len(origins) != 2 or len(profiles) != 2:
            raise ValueError("Configure two distinct Registry slots and runtime profiles")
        initial = config["initial_slot"]
        if initial not in self.registries:
            raise ValueError("Unknown initial Registry slot")
        self.default_ttl = config.get("default_ttl_seconds", 86400)
        self.max_ttl = config.get("max_ttl_seconds", 604800)
        if (
            type(self.default_ttl) is not int
            or type(self.max_ttl) is not int
            or not 1 <= self.default_ttl <= self.max_ttl
        ):
            raise ValueError("Invalid default or maximum retention TTL")
        self.terminal_receipt_seconds = config.get("terminal_receipt_seconds", 604800)
        if type(self.terminal_receipt_seconds) is not int or not 1 <= self.terminal_receipt_seconds < 2**63:
            raise ValueError("terminal_receipt_seconds must be a positive integer")
        self.runner_token = self.read_token(config["runner_token_file"])
        self.adx_origin = trusted_url(config["adx_origin"], origin=True)
        observer = urlsplit(self.adx_origin)
        if observer.scheme != "https" and observer.hostname not in {"127.0.0.1", "::1"}:
            raise ValueError("ADX instance observation requires HTTPS")
        self.adx_token_file = config["adx_token_file"]
        self.auto_delete = config.get("auto_delete", False)
        if type(self.auto_delete) is not bool:
            raise ValueError("auto_delete must be true or false")
        self.capacity = None
        if config.get("capacity") is not None:
            self.capacity = CapacityPolicy(**config["capacity"])
            self.capacity.validate(set(self.registries))
        with self.database_session() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA + CAPACITY_SCHEMA)
        with self.database_session(write=True) as db:
            hold_columns = {row["name"] for row in db.execute("PRAGMA table_info(holds)")}
            if "finished" not in hold_columns:
                db.execute("ALTER TABLE holds ADD COLUMN finished INTEGER NOT NULL DEFAULT 0")
            if "finished_at" not in hold_columns:
                db.execute("ALTER TABLE holds ADD COLUMN finished_at INTEGER")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(publications)")}
            if "pressure_evictable" not in columns:
                db.execute("ALTER TABLE publications ADD COLUMN pressure_evictable INTEGER NOT NULL DEFAULT 0")
            if "last_used_at" not in columns:
                db.execute("ALTER TABLE publications ADD COLUMN last_used_at INTEGER")
            if "planned_at" not in columns:
                db.execute("ALTER TABLE publications ADD COLUMN planned_at INTEGER")
                db.execute(
                    "UPDATE publications SET planned_at=? WHERE state='planned'",
                    (math.ceil(time.time()),),
                )
            if "prepare_until" not in columns:
                db.execute("ALTER TABLE publications ADD COLUMN prepare_until INTEGER")
            if "terminal_at" not in columns:
                db.execute("ALTER TABLE publications ADD COLUMN terminal_at INTEGER")
                db.execute(
                    """UPDATE publications SET terminal_at=?
                    WHERE state='deleted' OR (state='abandoned' AND tag IS NULL)""",
                    (math.ceil(time.time()),),
                )
            maintenance_columns = {row["name"] for row in db.execute("PRAGMA table_info(registry_maintenance)")}
            if "last_blocked_gc_slot" not in maintenance_columns:
                db.execute("ALTER TABLE registry_maintenance ADD COLUMN last_blocked_gc_slot TEXT REFERENCES slots(name)")
            db.execute(
                "CREATE INDEX IF NOT EXISTS publication_slot_state ON publications(slot, state)"
            )
            db.execute(
                """CREATE INDEX IF NOT EXISTS publication_terminal
                ON publications(terminal_at) WHERE terminal_at IS NOT NULL"""
            )
            db.execute(
                """CREATE INDEX IF NOT EXISTS publication_abandoned_gc
                ON publications(slot, retry_at, planned_at, id)
                WHERE state='abandoned' AND tag IS NOT NULL AND terminal_at IS NULL"""
            )
            db.executemany(
                "INSERT OR IGNORE INTO slots VALUES (?, ?, ?, ?)",
                [
                    (
                        name,
                        registry.origin,
                        registry.profile,
                        "active" if name == initial else "ready",
                    )
                    for name, registry in self.registries.items()
                ],
            )
            persisted = [
                (row["name"], row["origin"], row["profile"])
                for row in db.execute("SELECT * FROM slots")
            ]
            if set(persisted) != {(n, r.origin, r.profile) for n, r in self.registries.items()}:
                raise ValueError("Registry slot identity differs from durable state")
            if self.capacity is not None:
                db.execute(
                    """UPDATE publications SET prepare_until=?
                    WHERE state='new' AND prepare_until IS NULL""",
                    (math.ceil(time.time()) + self.capacity.prepare_seconds,),
                )
                db.executemany(
                    """INSERT OR IGNORE INTO registry_capacity
                    VALUES (?,?,-1,0,0,0,0)""",
                    self.capacity.storage_ids.items(),
                )
                identities = dict(db.execute("SELECT slot,storage_id FROM registry_capacity"))
                if identities != self.capacity.storage_ids:
                    raise ValueError("Registry capacity volume differs from durable state")
                db.execute(
                    "INSERT OR IGNORE INTO registry_maintenance(id,completed_at) VALUES (1,?)",
                    (int(time.time()),),
                )

    @staticmethod
    def read_token(path: str) -> str:
        token = Path(path).read_text().strip()
        if not token or re.search(r"\s", token):
            raise ValueError("Invalid service credential file")
        return token

    @contextmanager
    def database_session(self, *, write: bool = False):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            with db:
                yield db
        finally:
            db.close()

    def begin(self, ttl_seconds=None):
        ttl = self.default_ttl if ttl_seconds is None else ttl_seconds
        if type(ttl) is not int or not 1 <= ttl <= self.max_ttl:
            raise ValueError(f"ttl_seconds must be from 1 through {self.max_ttl}")
        publication = uuid.uuid4().hex
        with self.database_session(write=True) as db:
            slot = db.execute("SELECT * FROM slots WHERE state='active'").fetchone()
            if slot is None:
                raise CapacityUnavailable("Temporary Registries are paused for capacity maintenance; retry later")
            self.require_write_headroom(db, slot["name"])
            prepare_until = None
            if self.capacity is not None:
                now = time.time()
                db.execute(
                    """UPDATE publications SET state='abandoned', terminal_at=?
                    WHERE slot=? AND state='new' AND prepare_until<=?""",
                    (math.ceil(now), slot["name"], now),
                )
                in_flight = db.execute(
                    "SELECT COUNT(*) FROM publications WHERE slot=? AND state IN ('new','planned')",
                    (slot["name"],),
                ).fetchone()[0]
                if in_flight >= self.capacity.max_in_flight_builds:
                    raise CapacityUnavailable("Temporary Registry is at its concurrent build limit; retry later")
                prepare_until = math.ceil(now) + self.capacity.prepare_seconds
            db.execute(
                """INSERT INTO publications(id, slot, ttl, state, pressure_evictable, prepare_until)
                VALUES (?, ?, ?, 'new', ?, ?)""",
                (publication, slot["name"], ttl, self.lru_enabled, prepare_until),
            )
        result = {"id": publication, "tag": "build-" + publication, "profile": slot["profile"]}
        if prepare_until is not None:
            result["prepare_until"] = prepare_until
        return result

    @property
    def lru_enabled(self) -> bool:
        return self.capacity is not None and self.capacity.lru_idle_seconds is not None

    def require_write_headroom(self, db, slot: str):
        if self.capacity is None:
            return
        sample = db.execute("SELECT * FROM registry_capacity WHERE slot=?", (slot,)).fetchone()
        if not self.capacity.available(sample, int(time.time())):
            raise CapacityUnavailable("Temporary Registry has no verified write headroom; retry later")

    @staticmethod
    def publication(db, identity: str):
        row = db.execute("SELECT * FROM publications WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise ValueError("Unknown publication")
        return row

    def plan(self, publication: str, tag: str):
        repository, separator, suffix = tag.rpartition(":")
        if not separator or suffix != "build-" + publication:
            raise ValueError("Invalid publication tag")
        with self.database_session(write=True) as db:
            row = self.publication(db, publication)
            self.registries[row["slot"]].repository(repository)
            slot = db.execute("SELECT state FROM slots WHERE name=?", (row["slot"],)).fetchone()["state"]
            now = time.time()
            if row["state"] == "planned" and row["tag"] == tag:
                drain = db.execute(
                    "SELECT drain_until FROM registry_maintenance WHERE slot=?", (row["slot"],)
                ).fetchone()
                if slot in {"active", "draining"} and (drain is None or now < drain["drain_until"]):
                    return {}
                raise Conflict("Registry has fenced this publication; submit a new build")
            if (
                row["state"] in {"new", "abandoned"}
                and row["prepare_until"] is not None and now >= row["prepare_until"]
            ):
                raise CapacityUnavailable("Temporary build preparation reservation expired; retry later")
            if slot != "active":
                raise Conflict("Registry is draining; submit a new build")
            if row["state"] != "new":
                raise Conflict("Build preparation reservation is no longer active; submit a new build")
            self.require_write_headroom(db, row["slot"])
            if self.capacity is not None:
                planned = db.execute(
                    "SELECT COUNT(*) FROM publications WHERE slot=? AND state='planned'",
                    (row["slot"],),
                ).fetchone()[0]
                if planned >= self.capacity.max_in_flight_builds:
                    raise CapacityUnavailable("Temporary Registry is at its concurrent build limit; retry later")
            existing = db.execute(
                """SELECT id FROM publications WHERE tag IS NOT NULL
                AND substr(tag, 1, instr(tag, ':build-') - 1)=?""",
                (repository,),
            ).fetchone()
            if existing:
                raise Conflict("Publication repository is already reserved")
            db.execute(
                "UPDATE publications SET state='planned', tag=?, planned_at=? WHERE id=?",
                (tag, math.ceil(now), publication),
            )
        return {}

    def cancel_build(self, publication: str):
        with self.database_session(write=True) as db:
            db.execute(
                "UPDATE publications SET state='abandoned', terminal_at=? WHERE id=? AND state='new'",
                (math.ceil(time.time()), publication),
            )
        return {}

    def commit(self, publication: str, reference: str):
        with self.database_session() as db:
            row = self.publication(db, publication)
        repository = row["tag"].rsplit(":", 1)[0] if row["tag"] else None
        if (
            not isinstance(reference, str)
            or not re.fullmatch(DIGEST, reference.rpartition("@")[2])
            or reference.rpartition("@")[0] != repository
        ):
            raise ValueError("Expected the planned repository and an immutable digest")
        if row["state"] == "published" and row["reference"] == reference:
            return self.receipt(row)
        if (
            row["state"] != "planned"
            or self.registries[row["slot"]].resolve(row["tag"]) != reference
        ):
            raise Conflict("Registry digest does not match the planned publication")
        return self.record_digest(publication, reference)

    def record_digest(self, publication: str, reference: str):
        with self.database_session(write=True) as db:
            row = self.publication(db, publication)
            if row["state"] == "published" and row["reference"] == reference:
                return self.receipt(row)
            if row["state"] != "planned":
                raise Conflict("Publication is no longer awaiting a digest")
            expires_at = math.ceil(time.time()) + row["ttl"]
            db.execute(
                "UPDATE publications SET state='published', reference=?, expires_at=? WHERE id=?",
                (reference, expires_at, publication),
            )
        return {"expires_at": expires_at, "pressure_evictable": bool(row["pressure_evictable"])}

    @staticmethod
    def receipt(row):
        return {"expires_at": row["expires_at"], "pressure_evictable": bool(row["pressure_evictable"])}

    def acquire(self, reference: str, name: str):
        reference = reference.removeprefix("https://").removeprefix("http://")
        if not re.fullmatch(r"[^\s]+@" + DIGEST, reference) or not re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", name
        ):
            raise ValueError("An immutable image and a valid instance name are required")
        identity = uuid.uuid4().hex
        with self.database_session(write=True) as db:
            row = db.execute(
                "SELECT * FROM publications WHERE reference=?", (reference,)
            ).fetchone()
            if row is None or row["state"] != "published" or row["expires_at"] <= time.time():
                raise Conflict("Temporary image has expired or is unregistered; rebuild it")
            db.execute(
                "INSERT INTO holds(id, publication, instance) VALUES (?, ?, ?)",
                (identity, row["id"], "default-" + name),
            )
        return {"id": identity}

    def bind(self, hold: str, environment_id: str):
        with self.database_session() as db:
            row = db.execute(
                """SELECT p.reference FROM holds h JOIN publications p ON h.publication=p.id
                WHERE h.id=? AND h.instance=?""",
                (hold, environment_id),
            ).fetchone()
        state = self.observe(environment_id) if row else None
        if row is None or state is None or self.observed_reference(state) != row["reference"]:
            raise Conflict("The configured AKernel instance does not use this publication")
        with self.database_session(write=True) as db:
            result = db.execute(
                "UPDATE holds SET bound=1, retry_at=0 WHERE id=? AND instance=?",
                (hold, environment_id),
            )
            if result.rowcount != 1:
                raise Conflict("Sandbox identity does not match its image hold")
        return {}

    def cancel(self, hold: str):
        with self.database_session(write=True) as db:
            db.execute("DELETE FROM holds WHERE id=? AND bound=0", (hold,))
        return {}

    def finish(self, hold: str, environment_id: str):
        """Record a confirmed SDK stop before looking for directory absence."""
        with self.database_session(write=True) as db:
            db.execute(
                """UPDATE holds SET finished=1, finished_at=COALESCE(finished_at,?), retry_at=0
                WHERE id=? AND instance=? AND bound=1""",
                (math.ceil(time.time()), hold, environment_id),
            )
        return {}

    def release_stopped(self, hold: str):
        with self.database_session() as db:
            row = db.execute("SELECT instance FROM holds WHERE id=?", (hold,)).fetchone()
        if row is not None:
            if self.observe(row["instance"]) is not None:
                raise Conflict("The sandbox is still listed in the instance directory")
            with self.database_session(write=True) as db:
                self.release_hold(db, hold)
        return {}

    @staticmethod
    def release_hold(db, hold: str):
        row = db.execute("SELECT publication,finished_at FROM holds WHERE id=?", (hold,)).fetchone()
        if row is not None:
            db.execute(
                """UPDATE publications SET last_used_at=MAX(COALESCE(last_used_at,0),?)
                WHERE id=?""",
                (row["finished_at"] or math.ceil(time.time()), row["publication"]),
            )
            db.execute("DELETE FROM holds WHERE id=?", (hold,))

    @staticmethod
    def observed_reference(state: dict) -> str | None:
        image = state.get("image")
        if isinstance(image, str):
            return image.removeprefix("https://").removeprefix("http://")
        return None

    def observe(self, environment_id: str) -> dict | None:
        status, _, data = request(
            self.adx_origin + "/api/instances?" + urlencode({"instance_id": environment_id}),
            {
                "Authorization": "Bearer " + self.read_token(self.adx_token_file),
                "Cache-Control": "no-cache",
            },
        )
        if status == 404:
            detail = json.loads(data).get("error", {})
            if detail.get("code") == "NOT_FOUND" and detail.get("environmentId") == environment_id:
                return None
        elif status == 200:
            response = json.loads(data)
            if (
                isinstance(response, list)
                and len(response) == 1
                and response[0].get("id") == environment_id
            ):
                return response[0]
        raise ValueError(f"Instance status could not be confirmed (HTTP {status})")

    def reconcile_holds(self, slot: str | None = None):
        now = int(time.time())
        with self.database_session() as db:
            holds = db.execute(
                """SELECT h.*, p.reference FROM holds h JOIN publications p
                ON h.publication=p.id WHERE (p.expires_at<=? OR h.finished=1) AND h.retry_at<=?
                AND (? IS NULL OR p.slot=?)
                ORDER BY h.finished DESC, h.retry_at LIMIT 256""",
                (now, now, slot, slot),
            ).fetchall()
        for hold in holds:
            with self.database_session(write=True) as db:
                db.execute("UPDATE holds SET retry_at=? WHERE id=?", (now + 60, hold["id"]))
            try:
                state = self.observe(hold["instance"])
                if state is None and hold["finished"]:
                    with self.database_session(write=True) as db:
                        self.release_hold(db, hold["id"])
                elif state is not None and self.observed_reference(state) == hold["reference"]:
                    with self.database_session(write=True) as db:
                        db.execute("UPDATE holds SET bound=1 WHERE id=?", (hold["id"],))
            except (ValueError, OSError, AttributeError, TypeError) as exc:
                LOG.warning("Deferring hold %s (%s)", hold["id"], type(exc).__name__)

    def candidates(self, slot: str | None = None, idle_before: int | None = None):
        now = int(time.time())
        with self.database_session() as db:
            return db.execute(
                """SELECT p.* FROM publications p JOIN slots s ON s.name=p.slot
                WHERE (p.state='deleting' OR (p.state='published' AND (
                    p.expires_at<=? OR
                    (s.state='draining' AND p.pressure_evictable=1 AND p.last_used_at<=?)
                )))
                AND s.state!='sealed'
                AND (? IS NULL OR p.slot=?)
                AND p.retry_at<=?
                AND NOT EXISTS(SELECT 1 FROM holds h WHERE h.publication=p.id)
                ORDER BY CASE WHEN p.state='deleting' OR p.expires_at<=? THEN 0 ELSE 1 END,
                CASE WHEN p.expires_at<=? THEN p.expires_at ELSE p.last_used_at END, p.id LIMIT 256""",
                (now, idle_before, slot, slot, now, now, now),
            ).fetchall()

    def reclaim(self, row, idle_before: int | None = None):
        registry = self.registries[row["slot"]]
        if row["state"] == "published":
            worklist = registry.worklist(row["tag"], row["reference"])
            with self.database_session(write=True) as db:
                claim = db.execute(
                    """UPDATE publications SET state='deleting', worklist=?
                    WHERE id=? AND state='published' AND (
                        expires_at<=? OR (
                            pressure_evictable=1 AND last_used_at<=? AND last_used_at IS ?
                            AND EXISTS(SELECT 1 FROM slots WHERE name=publications.slot AND state='draining')
                        )
                    )
                    AND EXISTS(SELECT 1 FROM slots WHERE name=publications.slot AND state!='sealed')
                    AND NOT EXISTS(SELECT 1 FROM holds WHERE publication=?)""",
                    (json.dumps(worklist), row["id"], int(time.time()), idle_before,
                     row["last_used_at"], row["id"]),
                )
                if claim.rowcount != 1:
                    return
        else:
            worklist = json.loads(row["worklist"])
        repository = row["reference"].rsplit("@", 1)[0]
        for digest in worklist:
            registry.delete(repository, digest)
        with self.database_session(write=True) as db:
            db.execute(
                """UPDATE publications SET state='deleted', terminal_at=?, worklist=NULL
                WHERE id=? AND state='deleting'""",
                (math.ceil(time.time()), row["id"]),
            )
        LOG.info("Reclaimed manifests for %s in slot %s", row["id"], row["slot"])

    def sweep(self, *, apply: bool, slot: str | None = None, pressure: bool = False):
        if apply and not self.auto_delete:
            raise ValueError("Manifest deletion is disabled in configuration")
        idle_before = None
        if pressure and slot is not None and self.capacity is not None:
            idle = self.capacity.lru_idle_seconds
            if idle is not None:
                idle_before = int(time.time()) - idle
        self.reconcile_holds(slot)
        candidates = self.candidates(slot, idle_before)
        for row in candidates:
            if not apply:
                continue
            try:
                self.reclaim(row, idle_before)
            except (ValueError, OSError, KeyError, AttributeError, TypeError) as exc:
                LOG.warning("Deferring publication %s (%s)", row["id"], type(exc).__name__)
                with self.database_session(write=True) as db:
                    db.execute(
                        "UPDATE publications SET retry_at=? WHERE id=?",
                        (int(time.time()) + 60, row["id"]),
                    )
        return [{"id": r["id"], "slot": r["slot"], "reference": r["reference"]} for r in candidates]

    def prune_terminal(self):
        cutoff = int(time.time()) - self.terminal_receipt_seconds
        with self.database_session(write=True) as db:
            return db.execute(
                """DELETE FROM publications WHERE id IN (
                    SELECT p.id FROM publications p WHERE p.terminal_at<=?
                    AND p.state IN ('deleted','abandoned')
                    AND NOT EXISTS(SELECT 1 FROM holds h WHERE h.publication=p.id)
                    ORDER BY p.terminal_at, p.id LIMIT 256
                )""",
                (cutoff,),
            ).rowcount

    def abandoned_repositories(self, slot: str) -> dict[str, str]:
        with self.database_session() as db:
            rows = db.execute(
                """SELECT p.id, p.tag FROM publications p JOIN slots s ON p.slot=s.name
                WHERE p.slot=? AND s.state='sealed' AND p.state='abandoned' AND p.tag IS NOT NULL
                AND p.reference IS NULL AND p.terminal_at IS NULL AND p.retry_at<=?
                AND NOT EXISTS(SELECT 1 FROM holds h WHERE h.publication=p.id)
                ORDER BY p.retry_at, p.planned_at, p.id LIMIT ?""",
                (slot, int(time.time()), ABANDONED_BATCH_SIZE),
            ).fetchall()
        registry = self.registries[slot]
        return {registry.repository(row["tag"].rsplit(":", 1)[0]): row["id"] for row in rows}

    @staticmethod
    def record_abandoned_gc(db, abandoned: dict[str, str], cleaned: set[str]):
        now = math.ceil(time.time())
        db.executemany(
            """UPDATE publications SET terminal_at=?, retry_at=?
            WHERE id=? AND state='abandoned' AND terminal_at IS NULL""",
            [
                (now, 0, publication) if repo in cleaned else (None, now + 300, publication)
                for repo, publication in abandoned.items()
            ],
        )

    def status(self):
        with self.database_session() as db:
            slots = [dict(row) for row in db.execute("SELECT * FROM slots ORDER BY name")]
            counts = [
                dict(row)
                for row in db.execute(
                    "SELECT slot, state, COUNT(*) count FROM publications GROUP BY slot,state"
                )
            ]
            holds = [
                dict(row)
                for row in db.execute(
                    """SELECT bound, finished, COUNT(*) count
                    FROM holds GROUP BY bound,finished"""
                )
            ]
            unconfirmed = [
                dict(row)
                for row in db.execute(
                    "SELECT id, instance, publication FROM holds WHERE bound=0 LIMIT 256"
                )
            ]
            awaiting_stop = [
                dict(row)
                for row in db.execute(
                    """SELECT h.id, h.instance, h.publication
                    FROM holds h JOIN publications p ON p.id=h.publication
                    WHERE h.bound=1 AND h.finished=0 AND p.expires_at<=?
                    ORDER BY p.expires_at LIMIT 256""",
                    (int(time.time()),),
                )
            ]
            abandoned = [
                dict(row)
                for row in db.execute(
                    """SELECT slot, COUNT(*) AS count FROM publications
                    WHERE state='abandoned' AND tag IS NOT NULL AND terminal_at IS NULL
                    GROUP BY slot"""
                )
            ]
            capacity = [dict(row) for row in db.execute("SELECT * FROM registry_capacity ORDER BY slot")]
            maintenance = db.execute("SELECT * FROM registry_maintenance WHERE id=1").fetchone()
        result = {
            "slots": slots,
            "publications": counts,
            "holds": holds,
            "unconfirmed_holds": unconfirmed,
            "awaiting_stop_confirmation": awaiting_stop,
            "abandoned_repositories_pending_gc": abandoned,
        }
        if maintenance is not None:
            result["capacity"] = capacity
            result["maintenance"] = dict(maintenance)
        return result

    def switch(self, slot: str):
        with self.database_session(write=True) as db:
            self.switch_slot(db, slot, int(time.time()))
        return self.status()

    def switch_slot(self, db, slot: str, now: int):
        selected = db.execute("SELECT state FROM slots WHERE name=?", (slot,)).fetchone()
        if selected is None or selected["state"] != "ready":
            raise Conflict("The next Registry must be marked ready")
        if self.capacity is not None:
            sample = db.execute("SELECT * FROM registry_capacity WHERE slot=?", (slot,)).fetchone()
            if not self.capacity.available(sample, now):
                raise CapacityUnavailable("The standby Registry has no verified write headroom")
        active = db.execute("SELECT name FROM slots WHERE state='active'").fetchone()
        if active is not None:
            self.retire_slot(db, active["name"], now)
        db.execute("UPDATE slots SET state='active' WHERE name=?", (slot,))

    @staticmethod
    def retire_slot(db, slot: str, now: int):
        db.execute("UPDATE slots SET state='draining' WHERE name=?", (slot,))
        db.execute(
            "UPDATE publications SET state='abandoned', terminal_at=? WHERE slot=? AND state='new'",
            (now, slot),
        )

    def seal(self, slot: str):
        with self.database_session(write=True) as db:
            result = db.execute(
                "UPDATE slots SET state='sealed' WHERE name=? AND state IN ('draining','sealed')",
                (slot,),
            )
            if result.rowcount != 1:
                raise Conflict("Only a draining Registry can be sealed")
            pending = db.execute(
                "SELECT * FROM publications WHERE slot=? AND state='planned'", (slot,)
            ).fetchall()
        registry = self.registries[slot]
        for row in pending:
            reference = registry.resolve(row["tag"])
            if reference is None:
                with self.database_session(write=True) as db:
                    db.execute(
                        "UPDATE publications SET state='abandoned' WHERE id=? AND state='planned'",
                        (row["id"],),
                    )
            else:
                self.record_digest(row["id"], reference)
        return self.status()

    def ready(self, slot: str):
        with self.database_session(write=True) as db:
            self.ready_slot(db, slot)
        return self.status()

    @staticmethod
    def ready_slot(db, slot: str):
        result = db.execute(
            """UPDATE slots SET state='ready' WHERE name=? AND state='sealed'
            AND NOT EXISTS(SELECT 1 FROM publications WHERE slot=? AND state='planned')""",
            (slot, slot),
        )
        if result.rowcount != 1:
            raise Conflict("Seal and reconcile the Registry before making it ready")


def server_handler(retention: Retention):
    actions = {
        "/v1/begin": retention.begin,
        "/v1/plan": retention.plan,
        "/v1/cancel-build": retention.cancel_build,
        "/v1/commit": retention.commit,
        "/v1/acquire": retention.acquire,
        "/v1/bind": retention.bind,
        "/v1/cancel": retention.cancel,
        "/v1/finish": retention.finish,
    }

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(15)
            super().setup()

        def log_message(self, format, *args):
            pass

        def send(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            authorization = self.headers.get("Authorization", "")
            if not hmac.compare_digest(
                authorization.encode(), ("Bearer " + retention.runner_token).encode()
            ):
                self.send(401, {"error": "Invalid image retention credential"})
                return
            action = actions.get(self.path)
            if action is None:
                self.send(404, {"error": "Unknown image retention operation"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 1 <= size <= 8192:
                    raise ValueError("Invalid image retention request size")
                payload = json.loads(self.rfile.read(size))
                result = action(**payload)
            except Conflict as exc:
                self.send(409, {"error": str(exc)})
            except CapacityUnavailable as exc:
                self.send(503, {"error": str(exc), "code": "REGISTRY_CAPACITY_UNAVAILABLE"})
            except (TypeError, KeyError, AttributeError):
                self.send(400, {"error": "Invalid image retention request fields"})
            except ValueError as exc:
                self.send(400, {"error": str(exc)})
            except (OSError, sqlite3.Error) as exc:
                LOG.error("Image retention request failed (%s)", type(exc).__name__)
                self.send(503, {"error": "Image retention is temporarily unavailable"})
            else:
                self.send(200, result)

    return Handler


def serve(retention: Retention, args):
    if bool(args.tls_cert) != bool(args.tls_key):
        raise ValueError("TLS certificate and key must be provided together")
    if args.listen != "127.0.0.1" and not args.tls_cert:
        raise ValueError("Non-loopback retention endpoints require TLS")
    with ThreadingHTTPServer((args.listen, args.port), server_handler(retention)) as server:
        if args.tls_cert:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(args.tls_cert, args.tls_key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
        stopped = threading.Event()

        def sweep():
            while not stopped.wait(30):
                try:
                    retention.prune_terminal()
                    retention.sweep(apply=retention.auto_delete)
                except sqlite3.Error as exc:
                    LOG.error("Image retention sweep failed (%s)", type(exc).__name__)

        worker = threading.Thread(target=sweep, daemon=True)
        worker.start()
        try:
            server.serve_forever()
        finally:
            stopped.set()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    subcommands = parser.add_subparsers(dest="command", required=True)
    endpoint = subcommands.add_parser("serve")
    endpoint.add_argument("--listen", default="127.0.0.1")
    endpoint.add_argument("--port", type=int, default=8098)
    endpoint.add_argument("--tls-cert")
    endpoint.add_argument("--tls-key")
    subcommands.add_parser("status")
    maintain = subcommands.add_parser("maintain")
    maintain.add_argument("--once", action="store_true")
    cancel = subcommands.add_parser("cancel-hold")
    cancel.add_argument("hold")
    cancel.add_argument("--confirmed-absent", action="store_true", required=True)
    release = subcommands.add_parser("release-hold")
    release.add_argument("hold")
    release.add_argument("--confirmed-stopped", action="store_true", required=True)
    sweep = subcommands.add_parser("sweep")
    sweep.add_argument("--apply", action="store_true")
    for command in ("switch", "seal", "ready"):
        command_parser = subcommands.add_parser(command)
        command_parser.add_argument("slot")
        if command == "seal":
            command_parser.add_argument("--writers-fenced", action="store_true", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    retention = Retention(json.loads(args.config.read_text()), args.database)
    if args.command == "serve":
        serve(retention, args)
        return
    if args.command == "maintain":
        CapacityController(retention).run(once=args.once)
        return
    if args.command == "sweep":
        result = retention.sweep(apply=args.apply)
    elif args.command == "status":
        result = retention.status()
    elif args.command == "cancel-hold":
        result = retention.cancel(args.hold)
    elif args.command == "release-hold":
        result = retention.release_stopped(args.hold)
    else:
        with maintenance_lock(retention.database):
            with retention.database_session() as db:
                if db.execute(
                    "SELECT 1 FROM registry_maintenance WHERE slot IS NOT NULL"
                ).fetchone():
                    raise ValueError("Resume the queued Registry maintenance before manual rotation")
            result = getattr(retention, args.command)(args.slot)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
