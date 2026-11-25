"""Client for the optional AKernel build-image retention service."""

from __future__ import annotations

import os
import re
import ssl
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from ._backends.errors import BackendOperationError

PROFILE = "harbor-ephemeral"
_REPOSITORY = re.compile(r"akernel-ephemeral/[0-9a-f]{32}/service-0")


def temporary_reference(image: str | None) -> bool:
    if image is None:
        return False
    _, _, path = canonical_reference(image).partition("/")
    repository = path.partition("@")[0].partition(":")[0]
    return _REPOSITORY.fullmatch(repository) is not None


def canonical_reference(image: str) -> str:
    return image.removeprefix("https://").removeprefix("http://")


class Retention:
    """A runner uses a dedicated retention credential, separate from ADX."""

    def __init__(self) -> None:
        origin = os.environ.get("AKERNEL_IMAGE_RETENTION_URL", "")
        token_file = os.environ.get("AKERNEL_IMAGE_RETENTION_TOKEN_FILE", "")
        parsed = urlsplit(origin)
        loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        if (
            not parsed.hostname
            or (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback))
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or not token_file
        ):
            raise ValueError(
                "Temporary images require AKERNEL_IMAGE_RETENTION_URL and "
                "AKERNEL_IMAGE_RETENTION_TOKEN_FILE"
            )
        token = Path(token_file).read_text(encoding="utf-8").strip()
        if not token or re.search(r"\s", token):
            raise ValueError("The image retention credential is invalid")
        self.origin = origin.rstrip("/")
        self._token = token
        self._tls = ssl.create_default_context()

    def request(self, action: str, **payload: object) -> dict[str, Any]:
        try:
            response = httpx.post(
                f"{self.origin}/v1/{action}",
                json=payload,
                headers={"Authorization": f"Bearer {self._token}"},
                verify=self._tls,
                follow_redirects=False,
                trust_env=False,
                timeout=15,
            )
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BackendOperationError("Image retention request failed") from exc
        if not isinstance(body, dict):
            raise BackendOperationError("Invalid image retention response")
        if response.is_error:
            message = body.get("error")
            if not isinstance(message, str):
                message = "Image retention request failed"
            raise BackendOperationError(message)
        return body
