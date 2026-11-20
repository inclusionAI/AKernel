"""Durable managed image handles. Closing an SDK process never releases a lease."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import httpx

from ._backends.base import BackendConfig
from ._backends.errors import (
    AuthenticationException,
    BackendOperationError,
    NotFoundException,
    PermissionDeniedException,
)
from ._backends.registry import _config_from_env


@dataclass(frozen=True)
class ImageRetention:
    """Opt into managed image ownership; retained is the conservative policy.

    Ephemeral grace starts after the final explicit and platform references
    disappear, independently of image creation time. Units are seconds.
    """

    mode: str = "retained"
    grace_seconds: int = 86400

    def __post_init__(self) -> None:
        if self.mode not in {"retained", "ephemeral"}:
            raise ValueError("Image retention must be retained or ephemeral")
        if (
            isinstance(self.grace_seconds, bool)
            or not isinstance(self.grace_seconds, int)
            or not 0 <= self.grace_seconds <= 31536000
        ):
            raise ValueError("Image grace must be 0..31536000 seconds")

    @classmethod
    def ephemeral(cls, grace_seconds: int = 86400) -> ImageRetention:
        """Request reclamation after all references and the grace period end."""
        return cls("ephemeral", grace_seconds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            **(
                {"grace_seconds": self.grace_seconds}
                if self.mode == "ephemeral"
                else {}
            ),
        }


def _id(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("Invalid managed image identity")
    return value


class ManagedImage:
    """A server-owned publication, including all outputs of a Compose build.

    release() releases the original publisher lease, not other consumers.
    retain() uses a fresh lease ID; release that ID explicitly when finished.
    Snapshot and sandbox protection is maintained by the control plane.
    """

    def __init__(self, images: ManagedImages, view: dict[str, Any]):
        self._images = images
        self._view = view
        self.id = _id(view["image"]["id"])

    @property
    def info(self) -> dict[str, Any]:
        """Return a copy of the last receipt; refresh() queries current state."""
        import copy

        return copy.deepcopy(self._view)

    @property
    def reference(self) -> str:
        """Return the sole immutable output; multi-output builds use outputs."""
        outputs = self.outputs
        if len(outputs) != 1:
            raise ValueError("Publication does not have exactly one completed output")
        return next(iter(outputs.values()))

    @property
    def outputs(self) -> dict[str, str]:
        if self._view.get("summary"):
            self.refresh()
        return dict(self._view["image"]["outputs"])

    @property
    def state(self) -> str:
        return str(self._view["image"]["state"])

    def refresh(self) -> ManagedImage:
        self._view = self._images._request("GET", self.id)
        return self

    def _mutate(self, operation: str, **fields: Any) -> ManagedImage:
        self._view = self._images._request(
            "POST", self.id, {"operation": operation, **fields}
        )
        return self

    def _register_outputs(self, outputs: Mapping[str, str]) -> str:
        attempt_id = uuid4().hex
        self._mutate("register_outputs", outputs=dict(outputs), attempt_id=attempt_id)
        return attempt_id

    def _complete_publication(
        self, plan: Mapping[str, str], outputs: Mapping[str, str], attempt_id: str
    ) -> None:
        if plan.keys() != outputs.keys():
            raise ValueError("Runtime returned an incomplete publication receipt")
        digests = {}
        for name, reference in outputs.items():
            repository, marker, digest = reference.partition("@")
            if (
                not marker
                or repository != plan[name].rsplit(":", 1)[0]
                or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
            ):
                raise ValueError(
                    "Runtime returned a reference outside the publication plan"
                )
            digests[name] = digest
        self._mutate("complete", digests=digests, attempt_id=attempt_id)

    def cancel_preparation(self) -> ManagedImage:
        """Cancel before plan registration; a planned writer needs reconciliation."""
        return self._mutate("cancel_preparation")

    def retain(self, lease_id: str) -> ManagedImageLease:
        """Acquire an independent durable hold; released IDs cannot be reused."""
        self._mutate("retain", lease_id=lease_id)
        return ManagedImageLease(self, lease_id)

    def release(self, lease_id: str = "publisher") -> ManagedImage:
        """Idempotently release this hold; active and recovery consumers remain."""
        return self._mutate("release", lease_id=lease_id)

    def extend(self, retain_until: int) -> ManagedImage:
        """Extend the minimum retention UNIX timestamp without shortening it."""
        if (
            isinstance(retain_until, bool)
            or not isinstance(retain_until, int)
            or retain_until < 0
        ):
            raise ValueError("retain_until must be a nonnegative UNIX timestamp")
        return self._mutate("extend", retain_until=retain_until)

    def pin(self) -> ManagedImage:
        """Switch to permanent retention; set_retention() changes it explicitly."""
        return self._mutate("pin")

    def set_retention(self, retention: ImageRetention) -> ManagedImage:
        """Change policy against this receipt's revision; stale receipts conflict.

        Query current state after a conflict or a lost response before retrying.
        A successful change restarts any future unreferenced grace period.
        """
        return self._mutate(
            "set_retention",
            retention=retention.to_dict(),
            expected_revision=self._view["image"]["revision"],
        )


@dataclass(frozen=True)
class ManagedImageLease:
    """An independent caller hold, bound to the acquisition's image and cluster.

    release() always targets this lease. No destructor releases remote ownership.
    Persist image_id and lease_id to reconcile after a process restart.
    """

    _image: ManagedImage = field(repr=False, compare=False)
    lease_id: str

    @property
    def image_id(self) -> str:
        return self._image.id

    def release(self) -> ManagedImage:
        """Idempotently release this acquisition, preserving other holds."""
        return self._image.release(self.lease_id)


class ManagedImages:
    """Authenticated, tenant-scoped managed image inventory and ownership API."""

    def __init__(
        self, config: BackendConfig | None = None, *, tenant: str | None = None
    ):
        # Handles must never follow a later environment switch into another cluster.
        self._config = config if config is not None else _config_from_env()
        self._tenant = tenant

    def _request(
        self,
        method: str,
        identity: str = "",
        body: Mapping[str, Any] | None = None,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        config = self._config
        if identity:
            _id(identity)
        path = "/api/environment/v1/managed-images" + (
            "/" + identity if identity else ""
        )
        try:
            with httpx.Client(
                verify=config.verify_tls, timeout=30.0, follow_redirects=False
            ) as client:
                result = client.request(
                    method,
                    config.api_endpoint.base_url() + path,
                    headers={"X-Auth": config.token},
                    json=body,
                    params={
                        **(params or {}),
                        **(
                            {"tenant": self._tenant} if self._tenant is not None else {}
                        ),
                    },
                )
                if result.status_code >= 400:
                    error_type = {
                        401: AuthenticationException,
                        403: PermissionDeniedException,
                        404: NotFoundException,
                    }.get(result.status_code, BackendOperationError)
                    try:
                        detail = result.json().get("error", {})
                    except ValueError:
                        detail = {}
                    raise error_type(
                        f"Managed image request failed (HTTP {result.status_code}); "
                        "query the publication before retrying a build",
                        status_code=result.status_code,
                        code=detail.get("code"),
                        retry=detail.get("retry"),
                        outcome=detail.get("outcome"),
                    )
                value = result.json()
                if isinstance(value, dict) and value.get("code") == 200:
                    value = json.loads(base64.b64decode(value["data"], validate=True))
        except (httpx.HTTPError, ValueError) as error:
            raise BackendOperationError(
                "Managed image request outcome is unknown; "
                "query by the original request identity",
                outcome="unknown",
            ) from error
        if not isinstance(value, dict):
            raise BackendOperationError("Invalid managed image response")
        return value

    def begin(self, request_id: str, retention: ImageRetention) -> ManagedImage:
        """Register a publication before starting a builder; replay is idempotent."""
        if not isinstance(request_id, str) or not request_id or len(request_id) > 256:
            raise ValueError("request_id must contain 1..256 characters")
        return ManagedImage(
            self,
            self._request(
                "POST",
                body={
                    "operation": "begin",
                    "request_id": request_id,
                    "retention": retention.to_dict(),
                },
            ),
        )

    def get(self, image_id: str) -> ManagedImage:
        """Query a publication across SDK process lifetimes."""
        return ManagedImage(self, self._request("GET", _id(image_id)))

    def list(self, *, page_size: int = 100) -> Iterator[ManagedImage]:
        """Iterate bounded summaries; refresh a handle for its full receipt."""
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= 100
        ):
            raise ValueError("page_size must be 1..100")
        after = ""
        while True:
            page = self._request("GET", params={"after": after, "limit": page_size})
            for item in page["items"]:
                yield ManagedImage(self, item)
            next_token = page.get("next_page_token", "")
            if not next_token:
                break
            if not isinstance(next_token, str) or next_token <= after:
                raise BackendOperationError("Managed image pagination did not advance")
            after = _id(next_token)
