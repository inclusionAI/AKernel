"""Build submission and opaque runtime artifact handles; no Dockerfile parser."""

from __future__ import annotations

import json
import re
import shlex
import tarfile
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ._backends.errors import BackendOperationError, retain_failure_context
from ._managed_images import ImageRetention, ManagedImage, ManagedImages

if TYPE_CHECKING:
    from ._client import Client
    from .sandbox import Sandbox


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
    managed_image: ManagedImage | None = field(default=None, compare=False, repr=False)


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
    retention: ImageRetention | None = None,
    request_id: str | None = None,
    client: Client | None = None,
) -> BuiltImage:
    """Submit an original context through an authorized platform build profile.

    The platform supplies the builder image, endpoint, publication credentials
    and image provider. No host Docker daemon, helper binary or Registry route
    is needed on this client. The temporary preparation Sandbox is deleted on
    success/failure; unknown outcomes are not replayed. Cleanup errors preserve
    the preparation Sandbox identity. The returned image lifetime follows the
    platform profile's retention contract. Timeouts are seconds.

    Optional preparation CPU/memory requests use millicores/MiB. The existing
    preparation limits remain 1000 millicores and 2048 MiB; these settings do
    not change task resources or pretend the builder can use unlimited memory.

    retention explicitly enrolls the output in the server's managed image
    lifecycle. Keep request_id across interrupted calls to reconcile a lost
    response. A registered attempt is never automatically rebuilt. Its publisher
    hold survives SDK exit and is released through BuiltImage.managed_image.
    Omission retains the existing publication and lifetime behavior.
    """
    from ._docker_runtime import DockerRuntime
    from .sandbox import Sandbox

    options = DockerRuntime(profile=profile).sandbox_options()
    source = Path(context).resolve(strict=True)
    if not source.is_dir() or not (source / "Dockerfile").is_file():
        raise ValueError("Native build requires a context containing Dockerfile")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
        raise ValueError("Build timeout must be a positive integer")
    for name, request, limit in (
        ("cpu_request", cpu_request, 1000),
        ("memory_request", memory_request, 2048),
    ):
        if request is not None and (
            isinstance(request, bool)
            or not isinstance(request, int)
            or not 1 <= request <= limit
        ):
            raise ValueError(f"{name} must be an integer between 1 and {limit}")
    if request_id is not None and retention is None:
        raise ValueError("request_id requires explicit managed image retention")
    publication = None
    if retention is not None:
        images = client.images if client is not None else ManagedImages()
        identity = request_id or uuid.uuid4().hex
        try:
            publication = images.begin(identity, retention)
            if publication.state != "preparing":
                raise BackendOperationError(
                    "Publication already submitted; "
                    "query its managed image instead of rebuilding"
                )
        except BaseException as error:
            error.request_id = identity  # type: ignore[attr-defined]
            if publication is not None:
                error.publication_id = publication.id  # type: ignore[attr-defined]
            raise
        options["extra_config"] = {
            **options.get("extra_config", {}),
            "managedImagePublications": [publication.id],
        }
    failure: BaseException | None = None
    sandbox_type = client.Sandbox if client is not None else Sandbox
    try:
        builder = sandbox_type(
            cpu=1000 if cpu_request is None else cpu_request,
            memory=2048 if memory_request is None else memory_request,
            cpu_limit=1000,
            mem_limit=2048,
            idle_timeout=max(300, timeout + 60),
            schedule_timeout=schedule_timeout,
            **options,
        )
    except BaseException as error:
        if publication is not None:
            error.publication_id = publication.id  # type: ignore[attr-defined]
        raise
    try:
        upload_context(builder, source, "/work/context")
        publication_flags = ["--publication-id", publication.id] if publication else []
        if publication is not None:
            builder.commands.run(
                shlex.join(
                    [
                        "python3",
                        "/usr/lib/akernel/compose.py",
                        "image-build",
                        "--context",
                        "/work/context",
                        "--result",
                        "/work/publication-plan.json",
                        "--plan-only",
                        *publication_flags,
                    ]
                ),
                timeout=60,
                check=True,
            )
            plan = json.loads(
                builder.commands.run(
                    "cat /work/publication-plan.json", timeout=20, check=True
                ).stdout
            )
            publication._mutate("plan", outputs=plan["publication_plan"])
        result = builder.commands.run(
            shlex.join(
                [
                    "python3",
                    "/usr/lib/akernel/compose.py",
                    "image-build",
                    "--context",
                    "/work/context",
                    "--result",
                    "/work/artifact.json",
                    *(["--no-cache"] if no_cache else []),
                    *publication_flags,
                ]
            ),
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
        process = handle.get("image_process", {})
        if publication is not None:
            publication._mutate(
                "publish", digests={"single": reference.rsplit("@", 1)[1]}
            )
        return BuiltImage(
            reference=reference,
            envs=dict(
                value.split("=", 1) for value in process.get("Env", []) if "=" in value
            ),
            cwd=process.get("WorkingDir") or None,
            user=process.get("User") or None,
            has_startup_command=bool(process.get("Entrypoint") or process.get("Cmd")),
            managed_image=publication,
        )
    except BaseException as error:
        failure = error
        if publication is not None:
            error.publication_id = publication.id  # type: ignore[attr-defined]
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
        try:
            builder.kill()
        except BaseException as cleanup:
            if failure is not None:
                retain_failure_context(failure, builder.id, cleanup)
            else:
                cleanup_failure = BackendOperationError(
                    "Managed build preparation cleanup failed",
                    environment_id=builder.id,
                    operation_id=publication.id if publication is not None else None,
                    publication_id=publication.id if publication is not None else None,
                )
                retain_failure_context(cleanup_failure, builder.id, cleanup)
                raise cleanup_failure from cleanup
