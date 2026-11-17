"""Node runtime broker for Sandboxd-authorized per-environment collections."""

from __future__ import annotations

import hashlib
import fcntl
import hmac
import json
import logging
import os
import stat
import time
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from contextlib import contextmanager
from weakref import WeakValueDictionary

from image_provider import ImageProvider

logger = logging.getLogger(__name__)


class RuntimeCollections:
    def __init__(self, root: Path, image_socket: str):
        self.root = root
        self.image_socket = image_socket
        self.providers: WeakValueDictionary[str, ImageProvider] = WeakValueDictionary()
        self.lock = threading.Lock()

    def _record(self, authorization: str) -> dict:
        if not authorization.startswith("Bearer "):
            raise PermissionError("Runtime capability required")
        token = authorization.removeprefix("Bearer ")
        collection, _, _secret = token.partition(".")
        if not re.fullmatch(r"[a-f0-9]{32}", collection):
            raise PermissionError("Invalid runtime capability")
        path = self.root / "records" / (collection + ".json")
        try:
            if path.stat().st_mode & 0o077:
                raise PermissionError("Unsafe runtime authorization record")
            record = json.loads(path.read_text())
        except FileNotFoundError as error:
            raise PermissionError("Runtime capability unavailable") from error
        if not hmac.compare_digest(
            hashlib.sha256(token.encode()).hexdigest(), record["token_sha256"]
        ):
            raise PermissionError("Invalid runtime capability")
        if record["collection_id"] != collection:
            raise PermissionError("Runtime collection identity mismatch")
        if record.get("lifecycle_version", 0) not in {0, 1}:
            raise PermissionError("Unsupported runtime collection lifecycle")
        return record

    def authorize(self, authorization: str) -> ImageProvider:
        record = self._record(authorization)
        collection = record["collection_id"]
        with self.lock:
            provider = self.providers.get(collection)
            if provider is None:
                provider = ImageProvider(
                    self.root / "collections" / collection,
                    record["registry"],
                    image_socket=self.image_socket,
                    repository_prefix=record["repository_prefix"],
                    scheme=record["scheme"],
                    owner=collection,
                )
                self.providers[collection] = provider
            return provider

    @contextmanager
    def _lease(self, record: dict):
        if record.get("lifecycle_version", 0) == 0:
            # Old preview collections stay pinned until an operator drain.
            yield
            return
        identity = record["collection_id"]
        path = self.root / "leases" / identity
        try:
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        except FileNotFoundError as error:
            raise PermissionError("Runtime collection unavailable") from error
        try:
            deadline = time.monotonic() + 180
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            "Runtime collection operation wait timed out"
                        )
                    time.sleep(0.01)
            try:
                current = path.lstat()
            except FileNotFoundError as error:
                raise PermissionError("Runtime collection unavailable") from error
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
                or opened.st_nlink <= 1
                or (self.root / "retired" / identity).exists()
            ):
                raise PermissionError("Runtime collection retired or unowned")
            yield
        finally:
            os.close(fd)

    def perform(self, authorization: str, operation: str, reference: str) -> dict:
        record = self._record(authorization)
        with self._lease(record):
            # Revalidate after waiting: retirement may have revoked the record.
            provider = self.authorize(authorization)
            function = (
                provider.prepare
                if operation == "/prepare"
                else provider.describe_artifact
            )
            return function(reference)


def serve(collections: RuntimeCollections, port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path not in {"/prepare", "/describe"}:
                self.send_error(404)
                return
            try:
                authorization = self.headers.get("Authorization", "")
                collections._record(authorization)
            except PermissionError:
                self.send_error(401)
                return
            try:
                self.connection.settimeout(180)
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 16 * 1024:
                    raise ValueError("Invalid request size")
                body = json.loads(self.rfile.read(size))
                if set(body) != {"reference"} or not isinstance(body["reference"], str):
                    raise ValueError("Immutable artifact reference required")
                result = collections.perform(
                    authorization, self.path, body["reference"]
                )
                payload = json.dumps(result).encode()
            except PermissionError:
                self.send_error(401, "Runtime collection unavailable")
                return
            except ValueError as error:
                self.send_error(400, str(error))
                return
            except Exception:
                logger.exception(
                    "Managed image preparation failed; inspect the node runtime"
                )
                self.send_error(500, "Runtime image preparation failed")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--image-socket", default="/run/sandboxd/image-api/image.sock")
    parser.add_argument("--port", type=int, default=8840)
    args = parser.parse_args()
    serve(RuntimeCollections(args.root, args.image_socket), args.port)
