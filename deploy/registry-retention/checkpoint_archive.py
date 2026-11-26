"""Digest-addressed OCI data for distributed AKernel checkpoints."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from registry import ACCEPT, DIGEST, LIMIT, trusted_url

LOG = logging.getLogger(__name__)
RECEIPT = "akernel-image-archive.json"
VOLUME_MARKER = ".akernel-checkpoint-archive-volume"
REGISTRY_USER = "akernel-checkpoint"
ARTIFACT = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
LEASE = r"[0-9a-f]{32}"
ROUTE = re.compile(rf"/v2/akernel-checkpoint/({LEASE})/(manifests|blobs)/({DIGEST})")
NYDUS_BOOTSTRAP = "containerd.io/snapshot/nydus-bootstrap"
NYDUS_BLOB = "application/vnd.oci.image.layer.nydus.blob.v1"
MANIFEST_TYPES = ACCEPT.split(", ")
OCI_MEDIA = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9.+_-]*/[a-zA-Z0-9][a-zA-Z0-9.+_-]*")
CAPTURE_SECONDS = 3600
MAX_OBJECTS = 4096
BLOCK = 1024 * 1024
SCHEMA = """
CREATE TABLE IF NOT EXISTS archive_store (
    id INTEGER PRIMARY KEY CHECK(id=1), identity TEXT NOT NULL, scope TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS archive_images (
    digest TEXT PRIMARY KEY, graph TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS archive_checkpoints (
    artifact TEXT PRIMARY KEY, reference TEXT NOT NULL,
    publication TEXT REFERENCES publications(id), digest TEXT REFERENCES archive_images(digest)
);
CREATE INDEX IF NOT EXISTS archive_source_pin ON archive_checkpoints(publication);
CREATE INDEX IF NOT EXISTS archive_checkpoint_digest ON archive_checkpoints(digest);
CREATE TABLE IF NOT EXISTS archive_runtime (
    lease TEXT PRIMARY KEY, reference TEXT NOT NULL,
    digest TEXT NOT NULL REFERENCES archive_images(digest)
);
CREATE INDEX IF NOT EXISTS archive_runtime_digest ON archive_runtime(digest);
CREATE TABLE IF NOT EXISTS archive_released (
    lease TEXT PRIMARY KEY, expires_at REAL NOT NULL
);
"""


def sync_dir(path: Path):
    fd = os.open(path, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_atomic(path: Path, content: bytes):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".archive-", delete=False) as f:
        staged = Path(f.name)
        try:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
            os.replace(staged, path)
            sync_dir(path.parent)
        finally:
            staged.unlink(missing_ok=True)


def descriptor(value):
    digest = value["digest"]
    size = value["size"]
    media = value["mediaType"]
    if not re.fullmatch(DIGEST, digest) or type(size) is not int or size < 0:
        raise ValueError("Invalid OCI archive descriptor")
    if not isinstance(media, str) or not OCI_MEDIA.fullmatch(media) or value.get("urls"):
        raise ValueError("External OCI objects cannot be archived")
    return digest, size, media


def store_stream(source, target: Path, digest: str, expected: int, deadline: float):
    checksum = hashlib.sha256()
    size = 0
    with target.open("xb") as output:
        while chunk := source.read(BLOCK):
            if time.monotonic() >= deadline:
                raise TimeoutError("Checkpoint image copy timed out")
            size += len(chunk)
            if size > expected:
                raise ValueError("OCI archive object exceeds its descriptor size")
            output.write(chunk)
            checksum.update(chunk)
        if size != expected or "sha256:" + checksum.hexdigest() != digest:
            raise ValueError("OCI archive object failed its digest or size check")
        output.flush()
        os.fsync(output.fileno())


def inspect_bootstrap(binary: str, layer: Path, staging: Path):
    with tempfile.NamedTemporaryFile(dir=staging) as output:
        with tarfile.open(layer, "r:*") as archive:
            try:
                entry = archive.getmember("image/image.boot")
            except KeyError as exc:
                raise ValueError("A Nydus bootstrap layer is missing image/image.boot") from exc
            if not entry.isfile() or not 0 < entry.size <= 256 * LIMIT:
                raise ValueError("Invalid Nydus bootstrap file")
            with archive.extractfile(entry) as image:
                while chunk := image.read(BLOCK):
                    output.write(chunk)
        output.flush()
        try:
            result = subprocess.run(
                [binary, "inspect", "--bootstrap", output.name, "--request", "blobs"],
                check=True,
                capture_output=True,
                timeout=120,
                cwd=staging,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            raise ValueError("Could not verify the Nydus bootstrap") from exc
    entries = json.loads(result.stdout)
    if not isinstance(entries, list) or any(
        not isinstance(item, dict)
        or not re.fullmatch(r"[0-9a-f]{64}", item.get("blob_id", ""))
        for item in entries
    ):
        raise ValueError("Unrecognized Nydus bootstrap blob table")
    return {"sha256:" + item["blob_id"] for item in entries}


class CheckpointArchive:
    def __init__(self, retention, config):
        self.retention = retention
        self.mount = Path(config["mount"])
        self.artifacts = Path(config["checkpoint_artifacts"])
        self.root = Path(config["root"])
        self.volume_identity = config["volume_identity"]
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", self.volume_identity):
            raise ValueError("Set a nonempty JuiceFS archive volume identity")
        self.origin = trusted_url(config["registry_origin"], origin=True)
        url = urlsplit(self.origin)
        if url.scheme != "https" and url.hostname not in {"127.0.0.1", "::1"}:
            raise ValueError("The checkpoint archive Registry requires private HTTPS")
        self.authority = url.netloc
        self.worker_token = retention.read_token(config["worker_token_file"])
        registry_token = retention.read_token(config["registry_token_file"])
        if len({self.worker_token, registry_token, retention.runner_token}) != 3:
            raise ValueError("Checkpoint worker, Registry and runner credentials must be distinct")
        self.registry_authorization = b"Basic " + base64.b64encode(
            f"{REGISTRY_USER}:{registry_token}".encode()
        )
        self.nydus_image = config["nydus_image"]
        if not Path(self.nydus_image).is_absolute() or not os.access(self.nydus_image, os.X_OK):
            raise ValueError("Configure an executable nydus-image inspector")
        if (
            not all(path.is_absolute() and path.resolve() == path for path in
                    (self.mount, self.root, self.artifacts))
            or self.mount not in self.artifacts.parents
            or self.mount not in self.root.parents
            or self.root == self.artifacts
            or self.root in self.artifacts.parents
            or self.artifacts in self.root.parents
        ):
            raise ValueError("The archive and artifacts must be distinct paths on the configured JuiceFS mount")
        self.auto_delete = config.get("auto_delete", False)
        if type(self.auto_delete) is not bool:
            raise ValueError("Archive auto_delete must be true or false")
        self.lock = threading.Lock()
        self.active_captures: set[str] = set()
        self.copies = threading.BoundedSemaphore(4)
        self._check_volume()
        self.scope = self._artifact_scope()
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.objects = self.root / "objects"
        self.objects.mkdir(mode=0o700, exist_ok=True)
        if self.objects.resolve() != self.objects or self.objects.stat().st_dev != self.mount.stat().st_dev:
            raise ValueError("Archive objects must be on the configured JuiceFS mount")
        marker = self.root / "ledger"
        with retention.database_session(write=True) as db:
            row = db.execute("SELECT identity,scope FROM archive_store WHERE id=1").fetchone()
            if row is None:
                if marker.exists() or next(self.objects.iterdir(), None) is not None:
                    raise ValueError("Archive ledger is missing for an existing JuiceFS archive")
                identity = uuid.uuid4().hex
                db.execute("INSERT INTO archive_store VALUES (1, ?, ?)", (identity, self.scope))
            else:
                if row["scope"] != self.scope:
                    raise ValueError("Checkpoint store scope differs from the archive ledger")
                identity = row["identity"]
        self.identity = identity
        if not marker.exists() and next(self.objects.iterdir(), None) is None:
            write_atomic(marker, identity.encode())
        self.verify_volume()

    def _check_volume(self):
        # A missing FUSE mount must never look like an empty checkpoint directory.
        # mountinfo entries escape paths with octal escapes.
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            field, filesystem = line.split(" - ", 1)
            mountpoint = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), field.split()[4])
            if mountpoint == str(self.mount) and filesystem.split()[0] == "fuse.juicefs":
                if (
                    (self.mount / VOLUME_MARKER).read_text().strip() == self.volume_identity
                    and self.artifacts.is_dir()
                    and self.artifacts.resolve() == self.artifacts
                    and self.artifacts.stat().st_dev == self.mount.stat().st_dev
                ):
                    return
        raise OSError("The configured JuiceFS checkpoint volume is unavailable")

    def _artifact_scope(self):
        scope = (self.artifacts.parent / "store-scope").read_text()
        if not re.fullmatch(ARTIFACT, scope):
            raise ValueError("Invalid distributed checkpoint store scope")
        return scope

    def verify_volume(self):
        self._check_volume()
        if (
            (self.root / "ledger").read_text() != self.identity
            or self._artifact_scope() != self.scope
            or self.objects.stat().st_dev != self.mount.stat().st_dev
        ):
            raise OSError("The checkpoint archive volume or checkpoint scope does not match its ledger")

    @contextmanager
    def service_lock(self):
        path = self.retention.database.with_suffix(".archive.lock")
        with path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.verify_volume()
            for entry in self.root.glob(".copy-*"):
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
            yield

    def _artifact_dir(self, artifact: str):
        if not re.fullmatch(ARTIFACT, artifact):
            raise ValueError("Invalid checkpoint artifact ID")
        path = self.artifacts / artifact
        if not path.is_dir() or path.resolve() != path or path.stat().st_dev != self.mount.stat().st_dev:
            raise ValueError("Checkpoint artifact is unavailable on the configured JuiceFS volume")
        return path

    @staticmethod
    def _lease(lease: str):
        if not re.fullmatch(LEASE, lease):
            raise ValueError("Invalid checkpoint image runtime lease")
        return lease

    @staticmethod
    def _reference(reference: str):
        qualified, _, digest = reference.partition("@")
        if not re.fullmatch(DIGEST, digest):
            raise ValueError("Checkpoint image requires an immutable OCI digest")
        return qualified, digest

    @staticmethod
    def _record(db, artifact, reference, digest):
        row = db.execute(
            "SELECT reference, digest FROM archive_checkpoints WHERE artifact=?", (artifact,)
        ).fetchone()
        if row is None or row["reference"] != reference or row["digest"] != digest:
            raise ValueError("Checkpoint image receipt is not associated with this artifact")

    @staticmethod
    def _require_runtime(db, lease):
        if db.execute("SELECT 1 FROM archive_released WHERE lease=?", (lease,)).fetchone():
            raise ValueError("Checkpoint image runtime lease has been released")

    def _link_runtime(self, db, lease, reference, digest):
        self._require_runtime(db, lease)
        db.execute("INSERT OR IGNORE INTO archive_runtime VALUES (?, ?, ?)", (lease, reference, digest))
        row = db.execute("SELECT reference, digest FROM archive_runtime WHERE lease=?", (lease,)).fetchone()
        if row["reference"] != reference or row["digest"] != digest:
            raise ValueError("Checkpoint image runtime lease identity mismatch")

    def _copy_graph(self, registry, qualified, digest, staging, deadline):
        objects = {}
        manifests = set()
        nydus = 0
        pending = [(digest, None, None)]
        while pending:
            if time.monotonic() >= deadline:
                raise TimeoutError("Checkpoint image copy timed out")
            target, expected, media = pending.pop()
            if target in manifests:
                saved = objects["manifests/" + target]
                if expected is not None and saved != [expected, media]:
                    raise ValueError("OCI index describes a manifest inconsistently")
                continue
            status, headers, raw = registry.call(qualified, "manifests/" + target)
            content_type = headers.get("Content-Type", "").split(";", 1)[0]
            if status != 200 or content_type not in MANIFEST_TYPES or (
                expected is not None and (len(raw) != expected or content_type != media)
            ):
                raise ValueError("Checkpoint image has a missing or invalid OCI manifest")
            self._stage(objects, staging, "manifests", target, len(raw), content_type, io.BytesIO(raw), deadline)
            manifest = json.loads(raw)
            if manifest.get("schemaVersion") != 2:
                raise ValueError("Unsupported checkpoint image manifest schema")
            manifests.add(target)
            if content_type in (MANIFEST_TYPES[0], MANIFEST_TYPES[2]):
                children = manifest["manifests"]
                if not children or len(pending) + len(children) > MAX_OBJECTS:
                    raise ValueError("Invalid checkpoint image manifest index")
                pending.extend(descriptor(child) for child in children)
                continue
            layers = manifest["layers"]
            for value in [manifest["config"], *layers]:
                blob, size, blob_media = descriptor(value)
                key = "blobs/" + blob
                if key not in objects:
                    with registry.open_blob(qualified, blob) as response:
                        self._stage(objects, staging, "blobs", blob, size, blob_media, response, deadline)
                elif objects[key][0] != size:
                    raise ValueError("OCI manifest describes a blob inconsistently")
            if layers and NYDUS_BOOTSTRAP in layers[-1].get("annotations", {}):
                available = {
                    layer["digest"] for layer in layers[:-1] if layer["mediaType"] == NYDUS_BLOB
                }
                required = inspect_bootstrap(
                    self.nydus_image, staging / layers[-1]["digest"], staging
                )
                if not required <= available:
                    raise ValueError("Nydus bootstrap references data outside the archived OCI graph")
                nydus += 1
        if nydus == 0:
            raise ValueError("Checkpoint image has no Nydus manifest")
        return objects

    @staticmethod
    def _stage(objects, staging, kind, digest, size, media, data, deadline):
        if len(objects) >= MAX_OBJECTS:
            raise ValueError("Checkpoint image OCI graph exceeds the object limit")
        path = staging / digest
        if not path.exists():
            store_stream(data, path, digest, size, deadline)
        elif path.stat().st_size != size:
            raise ValueError("OCI archive object size mismatch")
        objects[kind + "/" + digest] = [size, media]

    def capture(self, artifact: str, reference: str, lease: str):
        if not re.fullmatch(ARTIFACT, artifact):
            raise ValueError("Invalid checkpoint artifact ID")
        deadline = time.monotonic() + CAPTURE_SECONDS
        with self.lock:
            if artifact in self.active_captures or not self.copies.acquire(blocking=False):
                raise OSError("Checkpoint image archive copy is already running or at its concurrency limit")
            self.active_captures.add(artifact)
        try:
            return self._capture(artifact, reference, lease, deadline)
        finally:
            with self.lock:
                self.active_captures.remove(artifact)
                self.copies.release()

    def _capture(self, artifact, reference, lease, deadline):
        self._lease(lease)
        qualified, digest = self._reference(reference)
        with self.lock:
            self.verify_volume()
            self._artifact_dir(artifact)
            with self.retention.database_session(write=True) as db:
                self._require_runtime(db, lease)
                if qualified == f"{self.authority}/akernel-checkpoint/{lease}":
                    active = db.execute(
                        "SELECT reference FROM archive_runtime WHERE lease=? AND digest=?", (lease, digest)
                    ).fetchone()
                    if active is None:
                        raise ValueError("Checkpoint source archive lease is unavailable")
                    reference = active["reference"]
                    publication = None
                else:
                    source = db.execute(
                        "SELECT id,slot FROM publications WHERE reference=? AND state='published'",
                        (reference,),
                    ).fetchone()
                    if source is None:
                        raise ValueError("Checkpoint source is no longer available in the temporary Registry")
                    publication = source["id"]
                db.execute(
                    "INSERT OR IGNORE INTO archive_checkpoints VALUES (?, ?, ?, NULL)",
                    (artifact, reference, publication),
                )
                existing = db.execute(
                    "SELECT reference,digest FROM archive_checkpoints WHERE artifact=?", (artifact,)
                ).fetchone()
                if existing["reference"] != reference:
                    raise ValueError("Checkpoint artifact already has a different image")
        try:
            with tempfile.TemporaryDirectory(dir=self.root, prefix=".copy-") as tmp:
                staging = Path(tmp)
                with self.lock:
                    self.verify_volume()
                    if self._publish(artifact, reference, digest, lease):
                        return {}
                if publication is None:
                    raise ValueError("Checkpoint source archive is missing")
                registry = self.retention.registries[source["slot"]]
                objects = self._copy_graph(registry, qualified, digest, staging, deadline)
                with self.lock:
                    self.verify_volume()
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Checkpoint image copy timed out")
                    if not self._publish(artifact, reference, digest, lease):
                        for key, (size, _) in objects.items():
                            target = key.split("/", 1)[1]
                            path = staging / target
                            stored = self.objects / target
                            if path.exists() and (not stored.is_file() or stored.stat().st_size != size):
                                os.replace(path, stored)
                        sync_dir(self.objects)
                        if not self._publish(artifact, reference, digest, lease, objects):
                            raise RuntimeError("Could not publish verified OCI graph")
            return {}
        except BaseException:
            with self.retention.database_session(write=True) as db:
                db.execute(
                    "DELETE FROM archive_checkpoints WHERE artifact=? AND digest IS NULL", (artifact,)
                )
            raise

    def _publish(self, artifact, reference, digest, lease, objects=None):
        directory = self._artifact_dir(artifact)
        with self.retention.database_session() as db:
            row = db.execute("SELECT graph FROM archive_images WHERE digest=?", (digest,)).fetchone()
        if row is not None:
            objects = json.loads(row["graph"])
        if objects is None:
            return False
        self._check_objects(objects)
        with self.retention.database_session(write=True) as db:
            if row is None:
                db.execute("INSERT INTO archive_images VALUES (?,?)", (digest, json.dumps(objects)))
            updated = db.execute(
                """UPDATE archive_checkpoints SET digest=?, publication=NULL
                WHERE artifact=? AND reference=? AND (digest IS NULL OR digest=?)""",
                (digest, artifact, reference, digest),
            )
            if updated.rowcount != 1:
                raise ValueError("Checkpoint image capture did not retain its source reservation")
            self._link_runtime(db, lease, reference, digest)
        write_atomic(
            directory / RECEIPT,
            json.dumps({"version": 1, "image": reference, "digest": digest}).encode() + b"\n",
        )
        return True

    def _check_objects(self, objects):
        for key, (size, _) in objects.items():
            path = self.objects / key.split("/", 1)[1]
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != size:
                raise OSError("Archived OCI image is incomplete")

    def restore(self, artifact: str, reference: str, lease: str):
        self._lease(lease)
        _, digest = self._reference(reference)
        with self.lock:
            self.verify_volume()
            receipt = self._artifact_dir(artifact) / RECEIPT
            if receipt.is_symlink() or receipt.stat().st_size > 8192 or json.loads(receipt.read_bytes()) != {
                "version": 1, "image": reference, "digest": digest
            }:
                raise ValueError("Checkpoint image archive does not match the source digest")
            with self.retention.database_session() as db:
                self._record(db, artifact, reference, digest)
                graph = db.execute(
                    "SELECT graph FROM archive_images WHERE digest=?", (digest,)
                ).fetchone()
            if graph is None:
                raise OSError("Archived OCI image is incomplete")
            self._check_objects(json.loads(graph["graph"]))
            with self.retention.database_session(write=True) as db:
                self._link_runtime(db, lease, reference, digest)
        return {"reference": f"{self.origin}/akernel-checkpoint/{lease}@{digest}"}

    def release(self, lease: str):
        self._lease(lease)
        with self.lock, self.retention.database_session(write=True) as db:
            # A timed-out capture may still be copying on the server. Fence
            # its final lease write even after the runtime has been deleted.
            db.execute(
                "INSERT OR REPLACE INTO archive_released VALUES (?,?)",
                (lease, time.time() + 2 * CAPTURE_SECONDS),
            )
            db.execute("DELETE FROM archive_runtime WHERE lease=?", (lease,))
        return {}

    def read(self, path):
        match = ROUTE.fullmatch(path)
        if not match:
            return None
        lease, kind, digest = match.groups()
        with self.lock:
            self.verify_volume()
            with self.retention.database_session() as db:
                row = db.execute(
                    """SELECT i.graph FROM archive_runtime r JOIN archive_images i ON i.digest=r.digest
                    WHERE r.lease=?""", (lease,)
                ).fetchone()
            key = kind + "/" + digest
            entry = json.loads(row["graph"]).get(key) if row is not None else None
            if entry is None:
                return None
            size, media = entry
            fd = os.open(self.objects / digest, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            contents = os.fdopen(fd, "rb")
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != size:
                contents.close()
                raise OSError("Archived OCI image object size changed")
            return contents, size, media, digest

    def sweep(self):
        with self.lock:
            self.verify_volume()
            with self.retention.database_session() as db:
                registered = {
                    row["artifact"] for row in db.execute(
                        "SELECT artifact FROM archive_checkpoints WHERE digest IS NULL OR ?",
                        (self.auto_delete,),
                    )
                }
            absent = []
            if registered:
                for artifact in registered.difference(os.listdir(self.artifacts)):
                    try:
                        (self.artifacts / artifact).lstat()
                    except FileNotFoundError:
                        absent.append((artifact,))
            if absent:
                self.verify_volume()
            with self.retention.database_session(write=True) as db:
                db.execute("DELETE FROM archive_released WHERE expires_at<?", (time.time(),))
                db.executemany("DELETE FROM archive_checkpoints WHERE artifact=?", absent)
                if not self.auto_delete:
                    return
                db.execute(
                    """DELETE FROM archive_images AS i WHERE NOT EXISTS (
                        SELECT 1 FROM archive_checkpoints c WHERE c.digest=i.digest
                    ) AND NOT EXISTS (
                        SELECT 1 FROM archive_runtime r WHERE r.digest=i.digest
                    )"""
                )
                marked = {
                    key.split("/", 1)[1]
                    for row in db.execute("SELECT graph FROM archive_images")
                    for key in json.loads(row["graph"])
                }
            for path in self.objects.iterdir():
                if re.fullmatch(DIGEST, path.name) and path.name not in marked:
                    path.unlink(missing_ok=True)
            sync_dir(self.objects)
