"""Allowlisted Distribution manifest operations for AKernel publications."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

PREFIX = "akernel-ephemeral"
TEMPORARY_REPOSITORY = re.compile(PREFIX + r"/[0-9a-f]{32}/service-0")
ABANDONED_BATCH_SIZE = 256
DIGEST = r"sha256:[0-9a-f]{64}"
ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
LIMIT = 16 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def trusted_url(value: str, *, origin: bool) -> str:
    url = urlsplit(value)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
        or (origin and url.path not in {"", "/"})
    ):
        raise ValueError("Expected an explicit HTTP(S) service URL")
    return value.rstrip("/")


def request(url: str, headers: dict[str, str], *, method: str = "GET"):
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(Request(url, headers=headers, method=method), timeout=15) as r:
            data = r.read(LIMIT + 1)
            if len(data) > LIMIT:
                raise ValueError("Service response exceeds the metadata limit")
            return r.status, r.headers, data
    except HTTPError as exc:
        with exc:
            data = exc.read(LIMIT + 1)
            if len(data) > LIMIT:
                raise ValueError("Service error exceeds the metadata limit") from exc
            return exc.code, exc.headers, data


class Registry:
    def __init__(self, config: dict):
        self.origin = trusted_url(config["origin"], origin=True)
        self.authority = urlsplit(self.origin).netloc
        self.profile = config["profile"]
        if not re.fullmatch(r"harbor-ephemeral-[a-z][a-z0-9-]{0,39}", self.profile):
            raise ValueError("Expected a dedicated managed runtime profile")
        realm = config.get("token_realm")
        self.realm = trusted_url(realm, origin=False) if realm else None
        self.service = config.get("token_service")
        self.credentials_file = config.get("credentials_file")
        if bool(self.realm) != bool(self.service) or bool(self.realm) != bool(
            self.credentials_file
        ):
            raise ValueError("Registry token realm, service and credentials are paired")

    def repository(self, value: str) -> str:
        authority, _, repository = value.partition("/")
        if authority != self.authority or not TEMPORARY_REPOSITORY.fullmatch(repository):
            raise ValueError("Image is outside this Registry's temporary build scope")
        return repository

    def headers(self, repository: str, method: str):
        headers = {"Accept": ACCEPT}
        if self.realm:
            credentials = json.loads(Path(self.credentials_file).read_text())
            basic = base64.b64encode(
                (credentials["username"] + ":" + credentials["password"]).encode()
            ).decode("ascii")
            actions = "pull,delete" if method == "DELETE" else "pull"
            query = urlencode(
                {"service": self.service, "scope": f"repository:{repository}:{actions}"}
            )
            status, _, data = request(f"{self.realm}?{query}", {"Authorization": f"Basic {basic}"})
            if status != 200:
                raise ValueError(f"Registry token exchange returned HTTP {status}")
            payload = json.loads(data)
            token = payload.get("token") or payload.get("access_token")
            if not isinstance(token, str) or not token or re.search(r"\s", token):
                raise ValueError("Registry issued an invalid token")
            headers["Authorization"] = "Bearer " + token
        return headers

    def call(self, qualified: str, suffix: str, *, method: str = "GET"):
        repository = self.repository(qualified)
        return request(
            f"{self.origin}/v2/{repository}/{suffix}",
            self.headers(repository, method),
            method=method,
        )

    def open_blob(self, qualified: str, digest: str):
        repository = self.repository(qualified)
        if not re.fullmatch(DIGEST, digest):
            raise ValueError("Invalid Registry blob digest")
        opener = build_opener(ProxyHandler({}), NoRedirect())
        return opener.open(
            Request(
                f"{self.origin}/v2/{repository}/blobs/{digest}",
                headers=self.headers(repository, "GET"),
            ),
            timeout=30,
        )

    def manifest(self, repository: str, target: str):
        if not re.fullmatch(DIGEST + r"|build-[0-9a-f]{32}", target):
            raise ValueError("Invalid Registry manifest target")
        status, headers, data = self.call(repository, "manifests/" + target)
        if status == 404:
            return None
        if status != 200:
            raise ValueError(f"Registry manifest read returned HTTP {status}")
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        if headers.get("Docker-Content-Digest", digest) != digest or (
            target.startswith("sha256:") and target != digest
        ):
            raise ValueError("Registry manifest digest mismatch")
        manifest = json.loads(data)
        if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 2:
            raise ValueError("Unsupported Registry manifest")
        return digest, manifest

    def resolve(self, tagged: str) -> str | None:
        repository, tag = tagged.rsplit(":", 1)
        manifest = self.manifest(repository, tag)
        return None if manifest is None else repository + "@" + manifest[0]

    def worklist(self, tagged: str, reference: str) -> list[str]:
        repository, tag = tagged.rsplit(":", 1)
        if self.resolve(tagged) != reference:
            raise ValueError("Publication tag changed or is missing")
        status, headers, data = self.call(repository, "tags/list?n=2")
        if status != 200 or headers.get("Link") or json.loads(data).get("tags") != [tag]:
            raise ValueError("Publication has unexpected Registry tags")
        pending = [reference.rsplit("@", 1)[1]]
        seen: dict[str, None] = {}
        while pending:
            digest = pending.pop()
            if digest in seen:
                continue
            if len(seen) >= 4096:
                raise ValueError("Publication manifest graph exceeds the limit")
            result = self.manifest(repository, digest)
            if result is None:
                raise ValueError("Publication manifest graph is incomplete")
            seen[digest] = None
            pending.extend(child["digest"] for child in result[1].get("manifests", []))
        return list(seen)

    def delete(self, repository: str, digest: str) -> None:
        if not re.fullmatch(DIGEST, digest):
            raise ValueError("Invalid Registry manifest digest")
        status, _, _ = self.call(repository, "manifests/" + digest, method="DELETE")
        if status not in {202, 404} or self.manifest(repository, digest) is not None:
            raise ValueError(f"Registry manifest deletion did not complete (HTTP {status})")
