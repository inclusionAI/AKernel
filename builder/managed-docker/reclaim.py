"""Operator manifest reclamation. Blob GC is a separate no-writer maintenance step.

Only explicit managed publication worklists are deleted. Neither repository
enumeration, blob deletion, nor direct Registry filesystem access is supported.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from akernel_sdk import ManagedImage, ManagedImages

ACCEPT = (
    "application/vnd.oci.image.index.v1+json, "
    "application/vnd.oci.image.manifest.v1+json, "
    "application/vnd.docker.distribution.manifest.list.v2+json, "
    "application/vnd.docker.distribution.manifest.v2+json"
)


class Registry:
    def __init__(
        self,
        origin: str,
        bearer: str = "",
        *,
        allow_http: bool = False,
        basic_auth: tuple[str, str] | None = None,
        repository_prefix: str | None = None,
    ):
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in ({"https", "http"} if allow_http else {"https"})
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("An explicit trusted Registry origin is required")
        self.origin = origin.rstrip("/")
        if bearer and basic_auth is not None:
            raise ValueError("Select exactly one Registry authentication method")
        self.authority = parsed.netloc
        if repository_prefix is not None and not all(
            re.fullmatch(r"[a-z0-9][a-z0-9._-]*", part)
            for part in repository_prefix.split("/")
        ):
            raise ValueError("Invalid managed Registry repository prefix")
        self.repository_prefix = repository_prefix
        self.client = httpx.Client(
            headers={"Authorization": "Bearer " + bearer} if bearer else {},
            auth=basic_auth,
            timeout=30,
            follow_redirects=False,
        )

    def close(self):
        self.client.close()

    def path(self, reference: str) -> str:
        repository, digest = reference.split("@", 1)
        if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
            raise ValueError("Invalid manifest digest")
        return self.manifest_path(repository, digest)

    def manifest_path(self, reference: str, target: str) -> str:
        registry, repository = reference.split("/", 1)
        if registry != self.authority or (
            self.repository_prefix is not None
            and not repository.startswith(self.repository_prefix + "/")
        ):
            raise ValueError("Publication is outside the selected Registry scope")
        if any(part in {"", ".", ".."} for part in repository.split("/")):
            raise ValueError("Invalid manifest repository")
        if any(c not in "abcdefghijklmnopqrstuvwxyz0123456789/._-" for c in repository):
            raise ValueError("Invalid manifest repository")
        return f"{self.origin}/v2/{repository}/manifests/{target}"

    def read_manifest(self, path: str):
        with self.client.stream("GET", path, headers={"Accept": ACCEPT}) as response:
            if response.status_code == 404:
                return None
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > 16 * 1024**2:
                    raise ValueError("Manifest exceeds metadata limit")
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        if response.headers.get("Docker-Content-Digest", digest) != digest:
            raise ValueError("Registry manifest digest header mismatch")
        manifest = json.loads(data)
        if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 2:
            raise ValueError("Unsupported manifest schema")
        return digest, manifest

    def read(self, reference: str):
        result = self.read_manifest(self.path(reference))
        if result is None:
            return None
        digest, manifest = result
        if digest != reference.rsplit("@", 1)[1]:
            raise ValueError("Manifest digest mismatch")
        return manifest

    def resolve_tag(self, tagged: str) -> str | None:
        repository, tag = tagged.rsplit(":", 1)
        if re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", tag) is None:
            raise ValueError("Invalid publication tag")
        result = self.read_manifest(self.manifest_path(repository, tag))
        return f"{repository}@{result[0]}" if result is not None else None

    def graph(self, roots: list[str]) -> list[str]:
        seen = set()
        pending = list(roots)
        while pending:
            reference = pending.pop()
            if reference in seen:
                continue
            seen.add(reference)
            if len(seen) > 4096:
                raise ValueError("Manifest graph exceeds limit")
            manifest = self.read(reference)
            if manifest is None:
                raise ValueError(
                    "Publication is incomplete; preserve it for reconciliation"
                )
            repository = reference.rsplit("@", 1)[0]
            for child in manifest.get("manifests", []):
                pending.append(repository + "@" + child["digest"])
        return sorted(seen)

    def delete(self, reference: str):
        response = self.client.delete(self.path(reference))
        if response.status_code not in {202, 404}:
            response.raise_for_status()
            raise ValueError("Registry did not acknowledge manifest deletion")
        if self.read(reference) is not None:
            raise ValueError("Manifest remains readable after deletion")
        return response.status_code == 202


def observed_outputs(image: ManagedImage, registry: Registry) -> dict[str, str]:
    result = {}
    for name, tagged in image.info["image"]["registered_outputs"].items():
        reference = registry.resolve_tag(tagged)
        if reference is not None:
            result[name] = reference
    return result


def reconcile_abandoned(
    image: ManagedImage, registry: Registry, writer_fence: str
) -> dict:
    image.refresh()
    receipt = image.info["image"]
    if image.state == "publishing":
        outputs = observed_outputs(image, registry)
        image._mutate(
            "reconcile_abandoned",
            digests={
                name: reference.rsplit("@", 1)[1] for name, reference in outputs.items()
            },
            writer_fence=writer_fence,
            expected_revision=receipt["revision"],
        )
    elif image.state != "abandoned" or receipt.get("writer_fence") != writer_fence:
        raise ValueError("Publication is outside the confirmed reconciliation attempt")
    return {"image_id": image.id, "state": image.state, "outputs": image.outputs}


def reclaim(image: ManagedImage, registry: Registry, *, apply: bool = False) -> dict:
    image.refresh()
    if image.state == "deleted":
        return {
            "image_id": image.id,
            "state": "deleted",
            "manifest_deletions": 0,
            "blob_gc": False,
        }
    if apply and not image.info["reclamation_enabled"]:
        raise ValueError("Managed image reclamation is disabled")
    if image.state == "deleting":
        manifests = image.info["image"]["deletion_manifests"]
    else:
        image._mutate("observe")
        if not image.info["eligible_for_reclamation"]:
            return {
                "image_id": image.id,
                "state": image.state,
                "protected": True,
                "view": image.info,
            }
        outputs = observed_outputs(image, registry)
        if outputs != image.outputs:
            raise ValueError("Registry tags do not match the publication receipt")
        manifests = registry.graph(list(outputs.values()))
    # Validate the entire persisted worklist before touching any manifest.
    for reference in manifests:
        registry.path(reference)
    if sum(len(reference) + 3 for reference in manifests) > 512 * 1024 - 1024:
        raise ValueError("Manifest worklist exceeds the publication byte budget")
    if not apply:
        return {
            "image_id": image.id,
            "dry_run": True,
            "manifests": manifests,
            "blob_gc": False,
        }
    image._mutate("claim_deletion", manifests=manifests)
    deleted_now = sum(registry.delete(reference) for reference in manifests)
    image._mutate("finish_deletion")
    return {
        "image_id": image.id,
        "state": image.state,
        "manifest_deletions": deleted_now,
        "manifest_already_absent": len(manifests) - deleted_now,
        "blob_gc": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument(
        "--repository-prefix", help="Exclusive managed Registry namespace"
    )
    credentials = parser.add_mutually_exclusive_group()
    credentials.add_argument("--registry-bearer-file", type=Path)
    credentials.add_argument(
        "--registry-basic-auth-file",
        type=Path,
        help="Protected JSON file with username and password for this Registry",
    )
    parser.add_argument(
        "--tenant", help="Explicit target tenant; requires an AKernel administrator"
    )
    parser.add_argument("--allow-http", action="store_true")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument(
        "--apply",
        action="store_true",
        help="Claim and delete the registered manifest worklist; default is dry-run",
    )
    operation.add_argument(
        "--reconcile-abandoned",
        action="store_true",
        help="Record present outputs after independently fencing the original writer",
    )
    parser.add_argument(
        "--writer-fence",
        help="Non-secret record of confirmed writer shutdown or revocation",
    )
    args = parser.parse_args()
    if (args.apply or args.reconcile_abandoned) and not args.repository_prefix:
        parser.error("--apply and --reconcile-abandoned require --repository-prefix")
    if args.reconcile_abandoned != bool(args.writer_fence):
        parser.error("--writer-fence is required only with --reconcile-abandoned")
    basic_auth = None
    if args.registry_basic_auth_file:
        path = args.registry_basic_auth_file
        if path.stat().st_mode & 0o077 or path.stat().st_size > 16384:
            raise ValueError("Registry credentials require a protected bounded file")
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or set(value) != {"username", "password"}:
            raise ValueError("Registry credentials require username and password")
        if any(not isinstance(v, str) or not v for v in value.values()):
            raise ValueError("Registry credentials must be nonempty strings")
        basic_auth = (value["username"], value["password"])
    registry = Registry(
        args.registry,
        args.registry_bearer_file.read_text().strip()
        if args.registry_bearer_file
        else "",
        allow_http=args.allow_http,
        basic_auth=basic_auth,
        repository_prefix=args.repository_prefix,
    )
    try:
        image = ManagedImages(tenant=args.tenant).get(args.image_id)
        result = (
            reconcile_abandoned(image, registry, args.writer_fence)
            if args.reconcile_abandoned
            else reclaim(image, registry, apply=args.apply)
        )
        print(json.dumps(result, sort_keys=True))
    finally:
        registry.close()


if __name__ == "__main__":
    main()
