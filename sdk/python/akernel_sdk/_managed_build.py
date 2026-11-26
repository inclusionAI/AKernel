"""Build submission and opaque runtime artifact handles; no Dockerfile parser."""

from __future__ import annotations

import json
import logging
import re
import shlex
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ._backends.errors import BackendOperationError, retain_failure_context
from ._ephemeral_images import (
    HARBOR_PROFILE,
    PROFILE,
    Retention,
    harbor_uses_checkpoint_archive,
)

if TYPE_CHECKING:
    from .sandbox import Sandbox

logger = logging.getLogger(__name__)

_PREPARATION_CPU_MILLICORES = 1000
_PREPARATION_MEMORY_MIB = 2048


@dataclass(frozen=True)
class BuiltImage:
    """Immutable image and public process defaults returned by the runtime.

    ``has_startup_command`` indicates whether its effective OCI ENTRYPOINT/CMD
    is nonempty. An image with neither can still host an interactive sandbox;
    leave ``Sandbox.inherit_entrypoint`` disabled for that image.
    """

    reference: str
    envs: dict[str, str]
    cwd: str | None = None
    user: str | None = None
    has_startup_command: bool = True
    expires_at: int | None = None
    pressure_evictable: bool = False


def upload_context(sandbox: Sandbox, source: Path, destination: str) -> None:
    """Transfer a directory as ordinary files/links, retaining native build inputs."""
    source = source.resolve(strict=True)
    if not source.is_dir():
        raise ValueError("Build context must be a directory")
    archive = destination + ".upload.tar.gz"
    with tempfile.TemporaryDirectory(prefix="akernel-context-") as temporary:
        local = Path(temporary) / "context.tar.gz"
        with tarfile.open(local, "w:gz", dereference=False) as output:
            output.add(source, arcname=".")
        sandbox.files.copy_from_local(str(local), archive)
    sandbox.commands.run(
        shlex.join(["mkdir", "-p", destination]), timeout=20, check=True
    )
    sandbox.commands.run(
        shlex.join(["tar", "-xzf", archive, "-C", destination]), timeout=120, check=True
    )
    sandbox.commands.run(shlex.join(["rm", "-f", archive]), timeout=20, check=True)


def build_image(
    context: str | Path,
    *,
    profile: str,
    no_cache: bool = False,
    timeout: int = 1800,
    schedule_timeout: int = 180,
    log_path: str | Path | None = None,
    cpu_request: int | None = None,
    memory_request: int | None = None,
    ttl_seconds: int | None = None,
) -> BuiltImage:
    """Submit an original context through an authorized platform build profile.

    The platform supplies the builder image, endpoint, publication credentials
    and image provider. No host Docker daemon, helper binary or Registry route
    is needed on this client. The temporary preparation Sandbox is deleted on
    success/failure; unknown outcomes are not replayed. Cleanup errors preserve
    the preparation Sandbox identity. The temporary profile allows an optional
    positive `ttl_seconds` override within the operator's limit. The reported
    `expires_at` is the Unix timestamp after which new consumers are rejected.
    If `pressure_evictable` is true, capacity maintenance can retire a consumed,
    idle image sooner. Running consumers keep the image protected. Timeouts are seconds.

    Optional preparation CPU and memory requests are positive integers in
    millicores and MiB. The SDK caps each request at the preparation sandbox's
    execution limit of 1000 millicores or 2048 MiB. Omitted requests use those
    limits as their defaults.
    """
    from ._docker_runtime import DockerRuntime
    from .sandbox import Sandbox

    source = Path(context).resolve(strict=True)
    if not source.is_dir() or not (source / "Dockerfile").is_file():
        raise ValueError("Native build requires a context containing Dockerfile")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
        raise ValueError("Build timeout must be a positive integer")
    for name, request in (
        ("cpu_request", cpu_request),
        ("memory_request", memory_request),
    ):
        if request is not None and (
            isinstance(request, bool)
            or not isinstance(request, int)
            or request < 1
        ):
            raise ValueError(f"{name} must be a positive integer")
    retention = None
    publication = None
    unified = profile == HARBOR_PROFILE and harbor_uses_checkpoint_archive()
    if profile == PROFILE or unified:
        if ttl_seconds is not None and (
            type(ttl_seconds) is not int or ttl_seconds < 1
        ):
            raise ValueError("ttl_seconds must be a positive integer")
        retention = Retention()
        if (
            unified
            and retention.request("archive-capabilities").get("checkpoint_archive")
            is not True
        ):
            raise BackendOperationError("The harbor-v1 image archive is unavailable")
        publication = retention.request("begin", ttl_seconds=ttl_seconds)
        profile = publication["profile"]
    elif isinstance(profile, str) and profile.startswith(PROFILE + "-"):
        raise ValueError(f"Temporary Registry slots are selected through {PROFILE!r}")
    elif ttl_seconds is not None:
        raise ValueError(f"ttl_seconds requires profile={PROFILE!r}")
    failure: BaseException | None = None
    builder = None
    publication_planned = False
    try:
        options = DockerRuntime(profile=profile).sandbox_options()
        builder = Sandbox(
            cpu=min(
                cpu_request or _PREPARATION_CPU_MILLICORES, _PREPARATION_CPU_MILLICORES
            ),
            memory=min(
                memory_request or _PREPARATION_MEMORY_MIB, _PREPARATION_MEMORY_MIB
            ),
            cpu_limit=_PREPARATION_CPU_MILLICORES,
            mem_limit=_PREPARATION_MEMORY_MIB,
            idle_timeout=max(300, timeout + 60),
            schedule_timeout=schedule_timeout,
            **options,
        )
        upload_context(builder, source, "/work/context")
        command = [
            "python3",
            "/usr/lib/akernel/compose.py",
            "image-build",
            "--context",
            "/work/context",
            "--result",
            "/work/artifact.json",
            *(["--no-cache"] if no_cache else []),
        ]
        if retention is not None and publication is not None:
            command += ["--publication-tag", publication["tag"]]
            builder.commands.run(
                shlex.join([*command, "--plan-only"]), timeout=20, check=True
            )
            planned = json.loads(
                builder.commands.run(
                    "cat /work/artifact.json", timeout=20, check=True
                ).stdout
            )
            retention.request(
                "plan", publication=publication["id"], tag=planned["tag"]
            )
            publication_planned = True
        result = builder.commands.run(
            shlex.join(command),
            timeout=timeout,
        )
        if log_path is not None:
            target = Path(log_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(result.stdout + result.stderr)
        from .commands import _checked_result

        _checked_result(result, True)
        handle = json.loads(
            builder.commands.run(
                "cat /work/artifact.json", timeout=20, check=True
            ).stdout
        )
        reference = handle["reference"]
        if (
            not isinstance(reference, str)
            or re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", reference) is None
        ):
            raise ValueError("Runtime returned an invalid artifact reference")
        expires_at = None
        pressure_evictable = False
        if retention is not None and publication is not None:
            receipt = retention.request(
                "commit", publication=publication["id"], reference=reference
            )
            expires_at = receipt["expires_at"]
            pressure_evictable = receipt.get("pressure_evictable") is True
        process = handle.get("image_process", {})
        return BuiltImage(
            reference=reference,
            envs=dict(
                value.split("=", 1) for value in process.get("Env", []) if "=" in value
            ),
            cwd=process.get("WorkingDir") or None,
            user=process.get("User") or None,
            has_startup_command=bool(process.get("Entrypoint") or process.get("Cmd")),
            expires_at=expires_at,
            pressure_evictable=pressure_evictable,
        )
    except BaseException as error:
        failure = error
        if (
            retention is not None
            and publication is not None
            and "prepare_until" in publication
            and not publication_planned
        ):
            try:
                retention.request("cancel-build", publication=publication["id"])
            except Exception:
                logger.warning("Could not release the build reservation", exc_info=True)
        if (
            isinstance(error, BackendOperationError)
            and error.command_result is not None
            and log_path is not None
        ):
            try:
                target = Path(log_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                partial = error.command_result
                target.write_text(partial.stdout + partial.stderr)
            except OSError as logging_error:
                add_note = getattr(error, "add_note", None)
                if callable(add_note):
                    add_note(
                        "Could not preserve partial build log: "
                        + type(logging_error).__name__
                    )
        raise
    finally:
        if builder is not None:
            try:
                builder.kill()
            except BaseException as cleanup:
                if failure is not None:
                    retain_failure_context(failure, builder.id, cleanup)
                else:
                    cleanup_failure = BackendOperationError(
                        "Managed build preparation cleanup failed",
                        environment_id=builder.id,
                    )
                    retain_failure_context(cleanup_failure, builder.id, cleanup)
                    raise cleanup_failure from cleanup
