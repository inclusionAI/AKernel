"""Startup-declared, immutable Compose images mounted by AKernel.

The snapshotter and guest bootstrap originate from tianyuzhou95's image-bridge
POC, agent-worker commit 9b3434d36086d920b6bb7f3322da642381a254b8.
Docker keeps ownership of container lifecycle; this module stages the mount plan.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


def _digest(value: Any) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None
    ):
        raise ValueError("Image bridge requires sha256 image and metadata digests")
    return value


def _chain_id(diffs: list[str]) -> str:
    if not diffs:
        raise ValueError("Mounted external images require a nonempty rootfs")
    chain = _digest(diffs[0])
    for diff in diffs[1:]:
        chain = (
            "sha256:" + hashlib.sha256(f"{chain} {_digest(diff)}".encode()).hexdigest()
        )
    return chain


def _validate_image_config(image: dict[str, Any], payloads: dict[str, Any]) -> None:
    """Bind the mounted chain to the selected immutable OCI configuration."""
    try:
        manifest = payloads[image["target"]["digest"]]
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
                raise ValueError("Image plan requires one linux/amd64 manifest")
            manifest = payloads[_digest(matches[0]["digest"])]
        expected = manifest["config"]
        if any(
            expected[k] != image["config"][k] for k in ("digest", "size", "mediaType")
        ):
            raise ValueError("Image config is not selected by its target manifest")
        config = payloads[_digest(expected["digest"])]
        if config.get("os") != "linux" or config.get("architecture") != "amd64":
            raise ValueError("Image config must match linux/amd64")
        rootfs = config["rootfs"]
        diffs = rootfs["diff_ids"]
        if (
            rootfs.get("type") != "layers"
            or not isinstance(diffs, list)
            or len(diffs) != len(manifest["layers"])
        ):
            raise ValueError("Image rootfs and manifest layer list disagree")
        if _chain_id(diffs) != image["chain_id"]:
            raise ValueError("Image chain ID does not match its rootfs configuration")
    except (KeyError, TypeError) as error:
        raise ValueError(
            "Image plan has incomplete OCI manifest/config metadata"
        ) from error


@dataclass(frozen=True)
class ImageMountPlan:
    directory: Path
    images: list[dict[str, Any]]
    services: dict[str, str]
    aliases: dict[str, str] = field(default_factory=dict)
    automatic: bool = False

    @classmethod
    def load(cls, path: str | Path, *, bases_only: bool = False) -> ImageMountPlan:
        path = Path(path).resolve()
        data = json.loads(path.read_text())
        images, services = data.get("images"), data.get("services")
        automatic = data.get("automatic", False)
        if not isinstance(automatic, bool):
            raise ValueError("Image plan automatic marker must be a boolean")
        if not isinstance(images, list) or (
            not images and not (bases_only and automatic)
        ):
            raise ValueError("Image plan requires a nonempty images list")
        if not isinstance(services, dict) or (
            "main" not in services and not bases_only
        ):
            raise ValueError("Image plan requires a services map including main")
        by_reference = {}
        for image in images:
            reference = image["reference"]
            digest = _digest(image["target"]["digest"])
            if (
                not reference.endswith("@" + digest)
                or image["root"] != "/opt/akernel-images/" + digest[7:]
                or reference in by_reference
            ):
                raise ValueError("Image reference, digest and immutable mount disagree")
            _digest(image["chain_id"])
            by_reference[reference] = image
            descriptors = set()
            payloads = {}
            for blob in image["blobs"]:
                name = blob["file"]
                if not isinstance(name, str) or Path(name).name != name:
                    raise ValueError("Image metadata must be a sibling filename")
                metadata = path.parent / name
                if metadata.is_symlink():
                    raise ValueError("Image metadata must not be a symlink")
                payload = metadata.read_bytes()
                descriptor = blob["descriptor"]
                actual = "sha256:" + hashlib.sha256(payload).hexdigest()
                if (
                    actual != _digest(descriptor["digest"])
                    or len(payload) != descriptor["size"]
                ):
                    raise ValueError("Image metadata digest or size mismatch")
                descriptors.add(actual)
                payloads[actual] = json.loads(payload)
            if not {digest, _digest(image["config"]["digest"])} <= descriptors:
                raise ValueError("Image plan is missing its manifest or config")
            _validate_image_config(image, payloads)
        for service, reference in services.items():
            if (
                not isinstance(service, str)
                or not service
                or reference not in by_reference
            ):
                raise ValueError(
                    "Every planned service must reference a declared image"
                )
        aliases = data.get("aliases", {})
        if not isinstance(aliases, dict) or any(
            not isinstance(alias, str) or not alias or reference not in by_reference
            for alias, reference in aliases.items()
        ):
            raise ValueError("Image aliases must reference declared immutable images")
        selected = (
            list(by_reference) if bases_only else list(dict.fromkeys(services.values()))
        )
        return cls(
            path.parent,
            [by_reference[r] for r in selected],
            dict(services),
            {
                alias: reference
                for alias, reference in aliases.items()
                if reference in selected
            },
            automatic,
        )

    def mounts(self, factory: Callable[..., Any]) -> list[Any]:
        # Different registry names can refer to the same immutable image root.
        by_root = {i["root"]: i for i in self.images}
        return [
            factory(target=i["root"], image_url=i["reference"])
            for i in by_root.values()
        ]
