"""Automatically prepare immutable image roots; Docker still performs the build.

The bridge's host-side discovery command uses compose-go and BuildKit's actual
Dockerfile parser. Only OCI manifests/configs pass through the SDK client.
Layer data is supplied by AKernel's image manager through read-only mounts.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from ._image_plan import ImageMountPlan, _chain_id, _digest

ACCEPT = (
    "application/vnd.oci.image.index.v1+json, "
    "application/vnd.oci.image.manifest.v1+json, "
    "application/vnd.docker.distribution.manifest.list.v2+json, "
    "application/vnd.docker.distribution.manifest.v2+json"
)


def _docker_credentials(host: str) -> tuple[str, str] | None:
    config = Path(os.environ.get("DOCKER_CONFIG", str(Path.home() / ".docker")))
    path = config / "config.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    server = "https://index.docker.io/v1/" if host == "docker.io" else host
    helper = data.get("credHelpers", {}).get(host) or data.get("credsStore")
    if helper:
        result = subprocess.run(
            ["docker-credential-" + helper, "get"],
            input=server,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"Docker credential helper failed for {host}")
        credential = json.loads(result.stdout)
        username, secret = credential["Username"], credential["Secret"]
        if not isinstance(username, str) or not isinstance(secret, str):
            raise ValueError("Invalid Docker credential helper response")
        return username, secret
    for name in (host, server, "https://" + host):
        auth = data.get("auths", {}).get(name, {}).get("auth")
        if auth:
            username, password = (
                base64.b64decode(auth, validate=True).decode().split(":", 1)
            )
            return username, password
    return None


class RegistryMetadata:
    def __init__(self, client: httpx.Client, directory: Path):
        self.client = client
        self.directory = directory
        self.tokens: dict[tuple[str, str], str] = {}

    def fetch(self, host: str, repository: str, kind: str, selector: str) -> bytes:
        endpoint = "registry-1.docker.io" if host == "docker.io" else host
        url = f"https://{endpoint}/v2/{repository}/{kind}/{selector}"
        key = (host, repository)
        headers = {"Accept": ACCEPT}
        if key in self.tokens:
            headers["Authorization"] = "Bearer " + self.tokens[key]
        response = self.client.get(url, headers=headers)
        if response.status_code == 401:
            challenge = response.headers.get("www-authenticate", "")
            credentials = _docker_credentials(host)
            if challenge.lower().startswith("bearer "):
                fields = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
                realm = fields.get("realm", "")
                if urlsplit(realm).scheme != "https":
                    raise ValueError("Registry token service must use HTTPS")
                token = self.client.get(
                    realm,
                    params={k: fields[k] for k in ("service", "scope") if k in fields},
                    auth=credentials,
                )
                token.raise_for_status()
                value = token.json().get("token") or token.json().get("access_token")
                if not isinstance(value, str) or not value:
                    raise ValueError("Registry returned no bearer token")
                self.tokens[key] = value
                headers["Authorization"] = "Bearer " + value
                response = self.client.get(url, headers=headers)
            elif challenge.lower().startswith("basic ") and credentials:
                response = self.client.get(url, headers=headers, auth=credentials)
        response.raise_for_status()
        payload = response.content
        actual = "sha256:" + hashlib.sha256(payload).hexdigest()
        expected = (
            selector
            if selector.startswith("sha256:")
            else response.headers.get("docker-content-digest")
        )
        if expected and actual != _digest(expected):
            raise ValueError("Registry metadata digest mismatch")
        return payload

    def prepare(self, reference: str) -> dict[str, Any]:
        repository, separator, selector = reference.partition("@")
        if not separator:
            repository, selector = reference.rsplit(":", 1)
        elif ":" in repository.rsplit("/", 1)[-1]:
            repository = repository.rsplit(":", 1)[0]
        host, name = repository.split("/", 1)
        blobs = []

        def fetch(kind: str, selected: str, descriptor: dict[str, Any] | None = None):
            payload = self.fetch(host, name, kind, selected)
            obj = json.loads(payload)
            digest = "sha256:" + hashlib.sha256(payload).hexdigest()
            if descriptor is not None and len(payload) != descriptor["size"]:
                raise ValueError("Registry metadata size mismatch")
            media_type = (descriptor or {}).get("mediaType") or obj["mediaType"]
            result = {"mediaType": media_type, "digest": digest, "size": len(payload)}
            filename = digest.replace(":", "-") + ".json"
            (self.directory / filename).write_bytes(payload)
            blobs.append({"descriptor": result, "file": filename})
            return obj, result

        manifest, target = fetch("manifests", selector)
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
                raise ValueError("Image must contain exactly one linux/amd64 manifest")
            manifest, _ = fetch("manifests", _digest(matches[0]["digest"]), matches[0])
        if "manifests" in manifest:
            raise ValueError("Image index nesting is too deep")
        descriptor = manifest["config"]
        config, config_descriptor = fetch(
            "blobs", _digest(descriptor["digest"]), descriptor
        )
        if config.get("os") != "linux" or config.get("architecture") != "amd64":
            raise ValueError("Image config must match linux/amd64")
        diffs = [_digest(d) for d in config["rootfs"]["diff_ids"]]
        if not diffs:
            raise ValueError("An external image with an empty rootfs cannot be mounted")
        chain = _chain_id(diffs)
        return {
            "reference": repository + "@" + target["digest"],
            "root": "/opt/akernel-images/" + target["digest"][7:],
            "chain_id": chain,
            "target": target,
            "config": config_descriptor,
            "blobs": blobs,
        }


def prepare_compose_images(
    directory: str | Path,
    files: Sequence[str | Path],
    *,
    output_dir: str | Path,
    bridge_binary: str | Path,
    env: Mapping[str, str] | None = None,
) -> ImageMountPlan:
    """Discover Compose/Dockerfile image inputs without building or pulling layers.

    The operator supplies a source-built, version-matched image-bridge helper.
    Credentials stay on the client. Sandboxd's existing image manager owns layer
    distribution, ROFS mounts and shared caching; this stores metadata only.
    Original project files are read without modification.
    """
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    request = {
        "directory": str(Path(directory).resolve()),
        "files": [str(Path(p).resolve()) for p in files],
    }
    binary = str(Path(bridge_binary).resolve())
    # Probe without project data before invoking a possibly older helper. The
    # schema version requires client-file containment before reading Dockerfiles.
    capability = subprocess.run(
        [binary, "--mode", "capabilities"],
        env=dict(env or {}),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    try:
        supported = (
            capability.returncode == 0
            and json.loads(capability.stdout).get("discover_schema") == 2
        )
    except (ValueError, AttributeError):
        supported = False
    if not supported:
        raise ValueError(
            "Image bridge helper must support discovery schema 2; "
            "install the helper matching this SDK"
        )
    result = subprocess.run(
        [binary, "--mode", "discover"],
        input=json.dumps(request),
        env=dict(env or {}),
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if result.returncode:
        raise ValueError("Automatic image discovery failed: " + result.stderr[-1500:])
    references = json.loads(result.stdout)["references"]
    aliases: dict[str, str] = {}
    images: dict[str, dict[str, Any]] = {}
    with httpx.Client(follow_redirects=True, timeout=60) as client:
        registry = RegistryMetadata(client, output)
        for reference in references:
            image = registry.prepare(reference)
            images.setdefault(image["reference"], image)
            aliases[reference] = image["reference"]
    path = output / "plan.json"
    path.write_text(
        json.dumps(
            {
                "images": list(images.values()),
                "services": {},
                "aliases": aliases,
                "automatic": True,
            },
            indent=2,
        )
    )
    return ImageMountPlan.load(path, bases_only=True)
