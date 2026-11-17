"""Node-side image preparation for a single owned managed Docker session.

The existing sandboxd image manager owns distribution and the shared ROFS.
This adapter exports its read-only roots through a runtime-owned collection.
The SDK sees artifact references and command results, never layer metadata.
This source-preview service is not a replacement for platform authorization.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import re
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
REFERENCE = re.compile(
    r"(?P<host>[^/]+)/(?P<repo>[a-z0-9][a-z0-9_./-]*)@(?P<digest>sha256:[0-9a-f]{64})\Z"
)


def digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def chain_id(values: list[str]) -> str:
    if not values or any(not DIGEST.fullmatch(v) for v in values):
        raise ValueError("Image requires valid rootfs diff IDs")
    result = values[0]
    for value in values[1:]:
        result = digest((result + " " + value).encode())
    return result


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_name(path.name + ".new")
    temporary.write_text(json.dumps(value, separators=(",", ":")))
    temporary.chmod(0o444)
    temporary.replace(path)


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost", timeout=180)
        self.path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class ImageProvider:
    def __init__(
        self,
        root: Path,
        registry: str,
        *,
        image_socket: str = "/run/sandboxd/image-api/image.sock",
        repository_prefix: str = "poc/",
        scheme: str = "https",
        owner: str | None = None,
    ):
        if scheme not in {"https", "http"}:
            raise ValueError("Registry scheme must be http or https")
        self.root = root
        self.registry = registry
        self.scheme = scheme
        self.image_socket = image_socket
        self.repository_prefix = repository_prefix
        self.owner = owner
        self.lock = threading.Lock()
        self.metadata = root / ".metadata"
        self.metadata.mkdir(mode=0o755, parents=True, exist_ok=True)
        self.plan = self.metadata / "plan.json"
        if not self.plan.exists():
            atomic_json(self.plan, {"images": [], "automatic": True})

    def validate_reference(self, reference: str) -> tuple[str, str]:
        match = REFERENCE.fullmatch(reference)
        if not match or match["host"] != self.registry:
            raise ValueError(
                "Expected an immutable artifact in the authorized Registry"
            )
        repository = match["repo"]
        if not repository.startswith(self.repository_prefix) or any(
            p in {"", ".", ".."} for p in repository.split("/")
        ):
            raise ValueError("Artifact is outside the session repository prefix")
        return repository, match["digest"]

    def fetch(
        self, repository: str, kind: str, selected: str, descriptor: dict | None = None
    ) -> tuple[dict, dict]:
        if kind not in {"manifests", "blobs"} or not DIGEST.fullmatch(selected):
            raise ValueError("Expected a digest-addressed metadata object")
        # Authentication belongs to Sandboxd's Registry client, including native
        # Bearer challenges, scoped token reuse and refresh. Never parse Registry
        # credentials or implement a second authentication protocol in this broker.
        connection = UnixConnection(self.image_socket)
        try:
            connection.request(
                "POST",
                "/registry_metadata",
                body=json.dumps(
                    {
                        "reference": f"{self.registry}/{repository}@{selected}",
                        "kind": kind,
                        "insecure": self.scheme == "http",
                    }
                ),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            if response.status != 200:
                raise RuntimeError(
                    f"Registry metadata preparation failed: HTTP {response.status}"
                )
            result = json.loads(response.read(24 * 1024**2))
            payload = base64.b64decode(result["data"], validate=True)
            media_type = result["media_type"]
        finally:
            connection.close()
        if len(payload) > 16 * 1024**2 or digest(payload) != selected:
            raise ValueError("Metadata size or digest mismatch")
        if descriptor and (
            descriptor["size"] != len(payload) or descriptor["digest"] != selected
        ):
            raise ValueError("Metadata descriptor mismatch")
        obj = json.loads(payload)
        desc = descriptor or {
            "mediaType": obj.get("mediaType", media_type),
            "digest": selected,
            "size": len(payload),
        }
        name = selected.replace(":", "-") + ".json"
        p = self.metadata / name
        if p.exists() and p.read_bytes() != payload:
            raise ValueError("Immutable metadata was replaced")
        if not p.exists():
            p.write_bytes(payload)
            p.chmod(0o444)
        return obj, {"descriptor": desc, "file": name}

    def describe(self, reference: str) -> dict:
        repository, selected = self.validate_reference(reference)
        manifest, blob = self.fetch(repository, "manifests", selected)
        target, blobs = blob["descriptor"], [blob]
        for _ in range(4):
            if "manifests" not in manifest:
                break
            matches = [
                d
                for d in manifest["manifests"]
                if d.get("platform", {}).get("os") == "linux"
                and d.get("platform", {}).get("architecture") == "amd64"
            ]
            if len(matches) != 1:
                raise ValueError("Expected exactly one linux/amd64 image")
            descriptor = matches[0]
            manifest, blob = self.fetch(
                repository, "manifests", descriptor["digest"], descriptor
            )
            blobs.append(blob)
        if "manifests" in manifest:
            raise ValueError("Image index nesting exceeds the limit")
        if not any(
            "nydus" in json.dumps(d).lower() for d in manifest.get("layers", [])
        ):
            raise ValueError("Managed artifact must contain Nydus layers")
        config_descriptor = manifest["config"]
        config, blob = self.fetch(
            repository, "blobs", config_descriptor["digest"], config_descriptor
        )
        blobs.append(blob)
        if config.get("os") != "linux" or config.get("architecture") != "amd64":
            raise ValueError("Image config platform mismatch")
        return {
            "reference": reference,
            "root": "/opt/akernel-images/" + selected[7:],
            "chain_id": chain_id(config["rootfs"]["diff_ids"]),
            "target": target,
            "config": blob["descriptor"],
            "blobs": blobs,
        }

    def mount_image(self, reference: str) -> str:
        connection = UnixConnection(self.image_socket)
        try:
            connection.request(
                "POST",
                "/oci_mount",
                body=json.dumps(
                    {
                        "image_url": reference,
                        **({"owner": self.owner} if self.owner else {}),
                    }
                ),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            payload = response.read(1024**2)
            if response.status != 200:
                raise RuntimeError(
                    f"sandboxd image preparation failed: HTTP {response.status}: "
                    + payload.decode()[-2000:]
                )
            mounted = json.loads(payload)["mount_path"]
        finally:
            connection.close()
        if not Path(mounted).is_dir():
            raise RuntimeError("Image manager returned an unavailable root")
        return mounted

    def validate_mount(self, destination: Path) -> None:
        result = subprocess.run(
            [
                "findmnt",
                "--noheadings",
                "--output",
                "OPTIONS",
                "--mountpoint",
                str(destination),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if "ro" not in result.stdout.strip().split(","):
            raise RuntimeError("Registered read-only root is unavailable or writable")

    def handle(self, image: dict, reused: bool) -> dict:
        result = {
            "reference": image["reference"],
            "plan": "/opt/akernel-images/.metadata/plan.json",
            "reused": reused,
        }
        result.update(self.process_defaults(image))
        return result

    def process_defaults(self, image: dict) -> dict:
        config = image.get("config", {}).get("digest")
        if config:
            return {
                "image_process": json.loads(
                    (self.metadata / (config.replace(":", "-") + ".json")).read_text()
                ).get("config", {})
            }
        return {}

    def describe_artifact(self, reference: str) -> dict:
        """Return verified build metadata without acquiring a ROFS mount.

        A single-container build helper does not run the task image. Its
        consumer acquires the final rootfs through Sandboxd's ordinary image
        lifecycle; registering that root here would retain an unused daemon.
        """
        with self.lock:
            image = self.describe(reference)
            return {"reference": reference, **self.process_defaults(image)}

    def prepare(self, reference: str) -> dict:
        self.validate_reference(reference)
        with self.lock:
            plan = json.loads(self.plan.read_text())
            for image in plan["images"]:
                if image["reference"] == reference:
                    self.validate_mount(self.root / reference.rsplit(":", 1)[1])
                    return self.handle(image, True)
            image = self.describe(reference)
            for existing in plan["images"]:
                if existing["root"] == image["root"]:
                    if any(
                        existing[key] != image[key]
                        for key in ["chain_id", "target", "config", "blobs"]
                    ):
                        raise ValueError("Immutable root metadata mismatch")
                    self.validate_mount(self.root / reference.rsplit(":", 1)[1])
                    plan["images"].append(image)
                    atomic_json(self.plan, plan)
                    return self.handle(image, True)
            mounted = self.mount_image(reference)
            destination = self.root / reference.rsplit(":", 1)[1]
            destination.mkdir(mode=0o755, exist_ok=True)
            subprocess.run(
                ["mount", "--bind", mounted, str(destination)], check=True, timeout=15
            )
            try:
                subprocess.run(
                    ["mount", "-o", "remount,bind,ro", str(destination)],
                    check=True,
                    timeout=15,
                )
                plan["images"].append(image)
                atomic_json(self.plan, plan)
            except BaseException:
                subprocess.run(["umount", str(destination)], check=True, timeout=15)
                raise
            return self.handle(image, False)


def serve(provider: ImageProvider, token_file: Path, address: tuple[str, int]) -> None:
    token = token_file.read_text().strip()
    if len(token) < 32 or token_file.stat().st_mode & 0o077:
        raise ValueError(
            "Session capability must be private and at least 32 characters"
        )

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path not in {"/prepare", "/describe"}:
                self.send_error(404)
                return
            if not hmac.compare_digest(
                self.headers.get("Authorization", ""), "Bearer " + token
            ):
                self.send_error(401)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size < 1 or size > 16 * 1024:
                    raise ValueError("Invalid preparation request length")
                body = json.loads(self.rfile.read(size))
                if set(body) != {"reference"} or not isinstance(body["reference"], str):
                    raise ValueError("Only an immutable artifact reference is accepted")
                operation = (
                    provider.prepare
                    if self.path == "/prepare"
                    else provider.describe_artifact
                )
                value = operation(body["reference"])
            except ValueError as error:
                self.reply(400, {"error": str(error), "phase": "validate_artifact"})
                return
            except Exception as error:  # noqa: BLE001 - HTTP boundary retains failure phase
                self.reply(500, {"error": str(error), "phase": "prepare_artifact"})
                return
            self.reply(200, value)

        def reply(self, status: int, value: object) -> None:
            payload = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    ThreadingHTTPServer(address, Handler).serve_forever()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--prefix", default="poc/")
    parser.add_argument("--scheme", choices=["https", "http"], default="https")
    parser.add_argument("--port", type=int, default=8840)
    args = parser.parse_args()
    serve(
        ImageProvider(
            args.root,
            args.registry,
            repository_prefix=args.prefix,
            scheme=args.scheme,
        ),
        args.token_file,
        ("0.0.0.0", args.port),
    )
