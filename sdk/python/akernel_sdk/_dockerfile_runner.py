# Copyright (c) 2026 Ant Group Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Runner that translates parsed Dockerfile instructions into sandbox ops.

The runner walks a
:class:`~akernel_sdk._dockerfile.ParsedDockerfile`, maintaining an accumulated
``{envs, workdir, user}`` context, and drives the existing sandbox operations
(``commands.run``, ``files.copy_from_local``, ``files.make_dir``). No BuildKit,
no docker daemon, no registry push — execution happens inside the sandbox.
"""

from __future__ import annotations

import os
import posixpath
import shlex
import shutil
import tarfile
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from ._dockercontext import DockerContext, _ContextManifest
from ._dockerfile import (
    CmdInstruction,
    CopyInstruction,
    DockerfileBuildError,
    EntrypointInstruction,
    EnvInstruction,
    ExposeInstruction,
    FromInstruction,
    ParsedDockerfile,
    RunInstruction,
    UserInstruction,
    WorkdirInstruction,
    resolve_start_cmd,
)
from .commands import CommandHandle
from .sandbox import Sandbox


@dataclass(frozen=True)
class DockerfileApplyResult:
    start_cmd: tuple[str, ...] | None
    startup_command: CommandHandle | None
    entrypoint: tuple[str, ...] | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class _PlannedCopy:
    """One selected context entry and its final sandbox target."""

    source_path: str
    relative_target: str
    remote_path: str
    kind: Literal["file", "directory"]
    mode: int
    is_destination_marker: bool = False
    local_path: str | None = None


@dataclass(frozen=True)
class _TarEntry:
    """One validated regular file or directory in a local ADD archive."""

    path: str
    is_dir: bool


@dataclass(frozen=True)
class _PreparedCopy:
    """A fully materialized COPY or ADD plan with no sandbox side effects."""

    index: int
    instruction: CopyInstruction
    workdir: str
    dest: str
    plans: tuple[_PlannedCopy, ...]
    extract_tar: bool
    tar_entries: tuple[_TarEntry, ...] = ()


# Default polling cadence for sandbox readiness before dispatching CMD.
_SANDBOX_READY_POLL_INTERVAL = 0.5
_SANDBOX_READY_POLL_TIMEOUT = 120
_TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".txz")


def apply_dockerfile(
    sb: Sandbox,
    parsed: ParsedDockerfile,
    context: DockerContext,
    *,
    auto_start_cmd: bool = True,
    run_timeout: int = 600,
) -> DockerfileApplyResult:
    """Execute a parsed Dockerfile's build-time instructions inside ``sb``.

    Walks ``parsed.instructions`` in order, translating each to sandbox
    operations. ``RUN``/``COPY``/``ADD`` failures raise
    :class:`DockerfileBuildError`.

    Args:
        sb: An already-launched sandbox (image = ``parsed.base_image``).
        parsed: Output of :func:`~akernel_sdk._dockerfile.parse_dockerfile`.
        context: The :class:`DockerContext` the parsed Dockerfile came from
            (used to read context files for COPY/ADD).
        auto_start_cmd: After build-time instructions, wait for sandbox readiness
            and dispatch ``CMD``/``ENTRYPOINT`` in the background. The returned
            command handle confirms dispatch only; no application health check is
            performed. Default ``True``.
        run_timeout: Per-``RUN`` timeout in seconds. Default ``600``.
    """
    if parsed.unsupported:
        kinds = ", ".join(sorted({item.kind for item in parsed.unsupported}))
        raise DockerfileBuildError(
            "Dockerfile contains unsupported instruction(s): " + kinds
        )
    if not parsed.base_image:
        raise ValueError("ParsedDockerfile.base_image is required to apply")

    runner = _Runner(sb, context, run_timeout)
    warnings = list(parsed.warnings)

    with tempfile.TemporaryDirectory() as staging_dir:
        prepared_copies: dict[int, _PreparedCopy] = {}
        planned_workdir = "/"
        for index, instruction in enumerate(parsed.instructions):
            if isinstance(instruction, WorkdirInstruction):
                planned_workdir = instruction.path
            elif isinstance(instruction, CopyInstruction):
                try:
                    prepared_copies[index] = runner.prepare_copy(
                        instruction, planned_workdir, index, staging_dir
                    )
                except DockerfileBuildError:
                    raise
                except Exception as exc:  # noqa: BLE001 — preserve Dockerfile location
                    raise DockerfileBuildError(
                        f"Instruction {index} failed: {exc}",
                        index=index,
                        instruction="ADD" if instruction.is_add else "COPY",
                    ) from exc

        # Drive the baseline runtime context after all COPY/ADD inputs are safe.
        envs: dict[str, str] = {}
        workdir = "/"
        user: str | None = None

        for index, instruction in enumerate(parsed.instructions):
            try:
                if isinstance(instruction, FromInstruction):
                    continue  # already used to launch the sandbox
                elif isinstance(instruction, RunInstruction):
                    runner.run(instruction.command, envs, workdir, user, index)
                elif isinstance(instruction, CopyInstruction):
                    runner.execute_prepared_copy(prepared_copies[index], envs)
                elif isinstance(instruction, EnvInstruction):
                    envs.update(instruction.envs)
                elif isinstance(instruction, WorkdirInstruction):
                    workdir = instruction.path
                    runner.ensure_workdir(workdir)
                elif isinstance(instruction, UserInstruction):
                    user = instruction.user
                elif isinstance(instruction, (CmdInstruction, EntrypointInstruction)):
                    continue  # handled after build-time instructions
                elif isinstance(instruction, ExposeInstruction):
                    continue  # metadata only
            except DockerfileBuildError:
                raise
            except Exception as exc:  # noqa: BLE001 — surface as build error
                raise DockerfileBuildError(
                    f"Instruction {index} failed: {exc}",
                    index=index,
                ) from exc

        # Resolve the effective start command: ENTRYPOINT + CMD per OCI semantics.
        start_cmd = _resolve_start_cmd(parsed)

        startup_command: CommandHandle | None = None

        # Sandbox-readiness gate before dispatching the resolved command.
        if auto_start_cmd and start_cmd is not None:
            runner.wait_sandbox_ready(_SANDBOX_READY_POLL_TIMEOUT)
            try:
                startup_command = runner.launch_start_cmd(
                    start_cmd, envs, workdir, user
                )
            except Exception as exc:  # noqa: BLE001 - attach Dockerfile metadata
                raise DockerfileBuildError(
                    "Failed to dispatch startup command: " + str(exc), instruction="CMD"
                ) from exc

        return DockerfileApplyResult(
            start_cmd=start_cmd,
            startup_command=startup_command,
            entrypoint=parsed.entrypoint,
            warnings=tuple(warnings),
        )


# ---------------------------------------------------------------------------
# Internal runner
# ---------------------------------------------------------------------------


class _Runner:
    def __init__(self, sb: Sandbox, context: DockerContext, run_timeout: int) -> None:
        self._sb = sb
        self._context = context
        self._run_timeout = run_timeout
        self._manifest: _ContextManifest | None = None

    def _context_manifest(self) -> _ContextManifest:
        if self._manifest is not None:
            return self._manifest
        try:
            self._manifest = _ContextManifest.from_context(self._context)
        except Exception as error:
            cause = error.__cause__
            detail = f"{error}: {cause}" if cause is not None else str(error)
            raise DockerfileBuildError(
                f"Failed to build Docker context manifest for "
                f"{type(self._context).__name__}: {detail}"
            ) from error
        return self._manifest

    # -- RUN --------------------------------------------------------------
    def run(
        self,
        command: str,
        envs: dict[str, str],
        workdir: str,
        user: str | None,
        index: int,
    ) -> None:
        wrapped = wrap_user(command, user)
        result = self._sb.commands.run(
            wrapped,
            envs=envs or None,
            cwd=workdir,
            timeout=self._run_timeout,
        )
        if result.exit_code != 0:
            raise DockerfileBuildError(
                f"RUN failed with exit code {result.exit_code}: {command[:120]}",
                index=index,
                instruction="RUN",
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
            )

    # -- COPY / ADD -------------------------------------------------------
    def prepare_copy(
        self,
        ins: CopyInstruction,
        workdir: str,
        index: int,
        staging_dir: str,
    ) -> _PreparedCopy:
        instruction = "ADD" if ins.is_add else "COPY"
        dest = self._copy_destination(ins.dest, workdir, index, instruction)
        manifest = self._context_manifest()
        selections = []
        for source in ins.srcs:
            try:
                selections.append((source, manifest.select(source)))
            except Exception as error:
                raise DockerfileBuildError(
                    f"{instruction} source {source!r} cannot be selected: {error}",
                    index=index,
                    instruction=instruction,
                ) from error

        plans: list[_PlannedCopy] = []
        for source, selection in selections:
            if (
                selection.kind == "wildcard"
                and selection.top_level_source_count > 1
                and not ins.dest.endswith("/")
            ):
                raise DockerfileBuildError(
                    f"{instruction} source {source!r} expands to multiple sources; "
                    "destination must end in '/'",
                    index=index,
                    instruction=instruction,
                )
            directory_target = (
                selection.kind in ("literal_directory", "dot")
                or selection.has_directories
                or ins.dest.endswith("/")
            )
            must_use_directory = selection.has_directories or len(selection.entries) > 1
            if (
                must_use_directory
                and not ins.dest.endswith("/")
                and not directory_target
            ):
                raise DockerfileBuildError(
                    f"{instruction} source {source!r} expands to multiple paths; "
                    "destination must end in '/'",
                    index=index,
                    instruction=instruction,
                )
            for selected in selection.entries:
                is_destination_marker = (
                    selected.kind == "directory" and selected.relative_target == ""
                )
                try:
                    remote_path = (
                        dest
                        if is_destination_marker
                        else self._copy_target(
                            dest, selected.relative_target, directory_target
                        )
                    )
                except Exception as error:
                    raise DockerfileBuildError(
                        f"{instruction} source {source!r} has an invalid target: "
                        f"{error}",
                        index=index,
                        instruction=instruction,
                    ) from error
                plans.append(
                    _PlannedCopy(
                        source_path=selected.source_path,
                        relative_target=selected.relative_target,
                        remote_path=remote_path,
                        kind=selected.kind,
                        mode=selected.mode,
                        is_destination_marker=is_destination_marker,
                    )
                )

        self._validate_copy_plan(plans, index, instruction)
        extract_tar = (
            ins.is_add
            and len(ins.srcs) == 1
            and selections[0][1].kind == "literal_file"
            and len(selections[0][1].entries) == 1
            and selections[0][1].entries[0].source_path.lower().endswith(_TAR_SUFFIXES)
        )
        materialized = self._materialize(
            plans, os.path.join(staging_dir, f"{index:08d}"), index, instruction
        )
        tar_entries = (
            self._read_tar_entries(materialized[0].local_path or "", index)
            if extract_tar
            else ()
        )
        return _PreparedCopy(
            index=index,
            instruction=ins,
            workdir=workdir,
            dest=dest,
            plans=tuple(materialized),
            extract_tar=extract_tar,
            tar_entries=tar_entries,
        )

    def execute_prepared_copy(
        self,
        prepared: _PreparedCopy,
        envs: dict[str, str],
    ) -> None:
        ins = prepared.instruction
        instruction = "ADD" if ins.is_add else "COPY"
        if prepared.extract_tar:
            archive = prepared.plans[0]
            tar_directories = self._tar_directories(prepared)
            self._ensure_tar_paths_not_symlinks(prepared, tar_directories)
            new_directories = (
                self._new_directories(tar_directories) if ins.chown else ()
            )
            self._extract_tar(
                archive.local_path or "",
                posixpath.basename(archive.source_path),
                prepared.dest,
                envs,
                prepared.workdir,
                prepared.index,
            )
            chown_targets = (
                tuple(
                    self._tar_remote_path(prepared.dest, entry.path)
                    for entry in prepared.tar_entries
                    if not entry.is_dir
                )
                + new_directories
            )
        else:
            directories = self._copy_directories(prepared.plans)
            new_directories = (
                self._new_directories(directories) if ins.chown else ()
            )
            for directory in directories:
                self._sb.files.make_dir(directory)
            for plan in prepared.plans:
                if plan.kind == "directory":
                    continue
                if plan.local_path is None:
                    raise AssertionError("copy plan was not materialized")
                self._sb.files.copy_from_local(plan.local_path, plan.remote_path)
            self._chmod(
                tuple(
                    dict.fromkeys(
                        (plan.remote_path, plan.mode)
                        for plan in prepared.plans
                        if not plan.is_destination_marker
                    )
                ),
                prepared.workdir,
                prepared.index,
                instruction,
            )
            chown_targets = (
                tuple(
                    dict.fromkeys(
                        plan.remote_path
                        for plan in prepared.plans
                        if plan.kind == "file"
                    )
                )
                + new_directories
            )

        if ins.chown:
            self._chown(
                ins.chown,
                chown_targets,
                prepared.workdir,
                prepared.index,
                instruction,
            )

    def _copy_destination(
        self, dest: str, workdir: str, index: int, instruction: str
    ) -> str:
        try:
            candidate = dest if dest.startswith("/") else _join_posix(workdir, dest)
            if "\0" in candidate or any(part == ".." for part in candidate.split("/")):
                raise ValueError("destination escapes the sandbox root")
            normalized = posixpath.normpath(candidate)
            if not posixpath.isabs(normalized):
                raise ValueError("destination is not absolute")
            return normalized
        except Exception as error:
            raise DockerfileBuildError(
                f"{instruction} destination {dest!r} is invalid: {error}",
                index=index,
                instruction=instruction,
            ) from error

    def _copy_target(self, dest: str, relative_target: str, directory: bool) -> str:
        candidate = posixpath.join(dest, relative_target) if directory else dest
        target = posixpath.normpath(candidate)
        if (
            not posixpath.isabs(target)
            or target == "/"
            or any(part == ".." for part in candidate.split("/"))
        ):
            raise ValueError(f"invalid COPY target: {target!r}")
        return target

    def _validate_copy_plan(
        self, plans: list[_PlannedCopy], index: int, instruction: str
    ) -> None:
        if not plans:
            raise DockerfileBuildError(
                f"{instruction} sources select no files",
                index=index,
                instruction=instruction,
            )
        relative_targets = [
            plan.relative_target for plan in plans if not plan.is_destination_marker
        ]
        if len(relative_targets) != len(set(relative_targets)):
            raise DockerfileBuildError(
                f"{instruction} sources have colliding relative targets",
                index=index,
                instruction=instruction,
            )
        by_target: dict[str, _PlannedCopy] = {}
        for plan in plans:
            existing = by_target.get(plan.remote_path)
            if existing is None:
                by_target[plan.remote_path] = plan
                continue
            if existing.is_destination_marker and plan.is_destination_marker:
                continue
            raise DockerfileBuildError(
                f"{instruction} sources have colliding destination paths",
                index=index,
                instruction=instruction,
            )
        for target, _plan in by_target.items():
            parent = posixpath.dirname(target)
            while parent != "/":
                ancestor = by_target.get(parent)
                if ancestor is not None and ancestor.kind == "file":
                    raise DockerfileBuildError(
                        f"{instruction} destination file conflicts with descendant: "
                        f"{parent!r}",
                        index=index,
                        instruction=instruction,
                    )
                parent = posixpath.dirname(parent)

    def _copy_directories(self, plans: tuple[_PlannedCopy, ...]) -> tuple[str, ...]:
        """Return parent and explicitly selected directories in creation order."""

        directories = set(
            self._parent_directories(plan.remote_path for plan in plans)
        )
        directories.update(
            plan.remote_path
            for plan in plans
            if plan.kind == "directory" and plan.remote_path != "/"
        )
        return tuple(sorted(directories, key=lambda path: (path.count("/"), path)))

    def _materialize(
        self,
        plans: list[_PlannedCopy],
        staging_dir: str,
        index: int,
        instruction: str,
    ) -> list[_PlannedCopy]:
        materialized: list[_PlannedCopy] = []
        os.makedirs(staging_dir, exist_ok=True)
        for sequence, plan in enumerate(plans):
            if plan.kind == "directory":
                materialized.append(plan)
                continue
            local_path = os.path.join(staging_dir, f"{sequence:08d}")
            try:
                with self._context.open(plan.source_path) as stream:
                    with open(local_path, "wb") as output:
                        shutil.copyfileobj(stream, output)
            except Exception as error:
                raise DockerfileBuildError(
                    f"{instruction} source {plan.source_path!r} could not be read: "
                    f"{error}",
                    index=index,
                    instruction=instruction,
                ) from error
            materialized.append(
                _PlannedCopy(
                    source_path=plan.source_path,
                    relative_target=plan.relative_target,
                    remote_path=plan.remote_path,
                    kind=plan.kind,
                    mode=plan.mode,
                    is_destination_marker=plan.is_destination_marker,
                    local_path=local_path,
                )
            )
        return materialized

    def _read_tar_entries(self, local_tar: str, index: int) -> tuple[_TarEntry, ...]:
        try:
            with tarfile.open(local_tar, "r:*") as archive:
                entries: list[_TarEntry] = []
                for member in archive.getmembers():
                    self._validate_tar_member_type(member, index)
                    entries.append(
                        _TarEntry(
                            path=self._normalize_tar_path(member.name, index),
                            is_dir=member.isdir(),
                        )
                    )
            validated_entries = tuple(entries)
            self._validate_tar_entries(validated_entries, index)
            return validated_entries
        except DockerfileBuildError:
            raise
        except (OSError, EOFError, tarfile.TarError) as error:
            raise DockerfileBuildError(
                f"ADD archive is invalid: {error}", index=index, instruction="ADD"
            ) from error

    def _validate_tar_member_type(self, member: tarfile.TarInfo, index: int) -> bool:
        if member.isreg() or member.isdir():
            return True
        raise DockerfileBuildError(
            f"ADD archive contains unsupported member type: {member.name!r}",
            index=index,
            instruction="ADD",
        )

    def _normalize_tar_path(self, path: str, index: int) -> str:
        if (
            not path
            or path.startswith("/")
            or "\\" in path
            or any(ord(character) < 32 or ord(character) == 127 for character in path)
            or any(part == ".." for part in path.split("/"))
        ):
            raise DockerfileBuildError(
                f"ADD archive contains unsafe entry: {path!r}",
                index=index,
                instruction="ADD",
            )
        normalized = posixpath.normpath(path)
        if normalized in ("", ".") or normalized.startswith("/"):
            raise DockerfileBuildError(
                f"ADD archive contains unsafe entry: {path!r}",
                index=index,
                instruction="ADD",
            )
        return normalized

    def _validate_tar_entries(self, entries: tuple[_TarEntry, ...], index: int) -> None:
        by_path: dict[str, _TarEntry] = {}
        for entry in entries:
            if entry.path in by_path:
                raise DockerfileBuildError(
                    f"ADD archive contains duplicate entry: {entry.path!r}",
                    index=index,
                    instruction="ADD",
                )
            by_path[entry.path] = entry
        for entry in entries:
            parent = posixpath.dirname(entry.path)
            while parent not in ("", "."):
                ancestor = by_path.get(parent)
                if ancestor is not None and not ancestor.is_dir:
                    raise DockerfileBuildError(
                        "ADD archive has a file-as-ancestor collision: "
                        f"{ancestor.path!r}",
                        index=index,
                        instruction="ADD",
                    )
                parent = posixpath.dirname(parent)
            if not entry.is_dir and any(
                other.path.startswith(entry.path + "/") for other in entries
            ):
                raise DockerfileBuildError(
                    f"ADD archive has a file-as-ancestor collision: {entry.path!r}",
                    index=index,
                    instruction="ADD",
                )

    def _parent_directories(self, paths: Iterable[str]) -> tuple[str, ...]:
        directories: set[str] = set()
        for path in paths:
            parent = posixpath.dirname(path)
            while parent != "/":
                directories.add(parent)
                parent = posixpath.dirname(parent)
        return tuple(sorted(directories, key=lambda path: (path.count("/"), path)))

    def _new_directories(self, directories: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            directory
            for directory in directories
            if not self._sb.files.exists(directory)
        )

    def _tar_remote_path(self, dest: str, path: str) -> str:
        return posixpath.normpath(posixpath.join(dest, path))

    def _tar_directories(self, prepared: _PreparedCopy) -> tuple[str, ...]:
        member_paths = tuple(
            self._tar_remote_path(prepared.dest, entry.path)
            for entry in prepared.tar_entries
        )
        directories = set(self._parent_directories(member_paths))
        directories.update(
            self._tar_remote_path(prepared.dest, entry.path)
            for entry in prepared.tar_entries
            if entry.is_dir
        )
        if prepared.dest != "/":
            directories.add(prepared.dest)
        return tuple(sorted(directories, key=lambda path: (path.count("/"), path)))

    def _ensure_tar_paths_not_symlinks(
        self, prepared: _PreparedCopy, directories: tuple[str, ...]
    ) -> None:
        member_paths = tuple(
            self._tar_remote_path(prepared.dest, entry.path)
            for entry in prepared.tar_entries
        )
        paths = tuple(dict.fromkeys((prepared.dest,) + member_paths + directories))
        command = " && ".join(f"test ! -L {shlex.quote(path)}" for path in paths)
        result = self._sb.commands.run(
            command,
            cwd=prepared.workdir,
            timeout=self._run_timeout,
        )
        if result.exit_code != 0:
            raise DockerfileBuildError(
                f"ADD archive destination path is a symlink: {result.stderr}",
                index=prepared.index,
                instruction="ADD",
                stderr=result.stderr,
                exit_code=result.exit_code,
            )

    def _chown(
        self,
        owner: str,
        targets: tuple[str, ...],
        workdir: str,
        index: int,
        instruction: str,
    ) -> None:
        for target in targets:
            command = f"chown {shlex.quote(owner)} {shlex.quote(target)}"
            result = self._sb.commands.run(
                wrap_user(command, "root"),
                cwd=workdir,
                timeout=self._run_timeout,
            )
            if result.exit_code != 0:
                raise DockerfileBuildError(
                    f"{instruction} --chown failed: {result.stderr}",
                    index=index,
                    instruction=instruction,
                    stderr=result.stderr,
                    exit_code=result.exit_code,
                )

    def _chmod(
        self,
        targets: tuple[tuple[str, int], ...],
        workdir: str,
        index: int,
        instruction: str,
    ) -> None:
        """Restore selected context entry modes without touching parent paths."""

        for target, mode in targets:
            command = f"chmod {mode:04o} {shlex.quote(target)}"
            result = self._sb.commands.run(
                wrap_user(command, "root"),
                cwd=workdir,
                timeout=self._run_timeout,
            )
            if result.exit_code != 0:
                raise DockerfileBuildError(
                    f"{instruction} chmod failed: {result.stderr}",
                    index=index,
                    instruction=instruction,
                    stderr=result.stderr,
                    exit_code=result.exit_code,
                )

    def _extract_tar(
        self,
        local_tar: str,
        archive_name: str,
        dest: str,
        envs: dict[str, str],
        workdir: str,
        index: int,
    ) -> None:
        # Docker ADD extraction creates the destination directory before tar xf.
        if dest != "/":
            self._sb.files.make_dir(dest)
        sandbox_tar = f"/tmp/akernel_add_{archive_name}"
        self._sb.files.copy_from_local(local_tar, sandbox_tar)
        tar_cmd = (
            f"tar xf {shlex.quote(sandbox_tar)} --no-same-owner -C {shlex.quote(dest)}"
        )
        result = self._sb.commands.run(
            wrap_user(tar_cmd, "root"),
            envs=envs or None,
            cwd=workdir,
            timeout=self._run_timeout,
        )
        if result.exit_code != 0:
            raise DockerfileBuildError(
                f"ADD tar extraction failed: {result.stderr}",
                index=index,
                instruction="ADD",
                stderr=result.stderr,
                exit_code=result.exit_code,
            )

    # -- WORKDIR ----------------------------------------------------------
    def ensure_workdir(self, workdir: str) -> None:
        if workdir and workdir != "/":
            self._sb.files.make_dir(workdir)

    # -- Sandbox readiness + CMD dispatch ---------------------------------
    def wait_sandbox_ready(self, timeout: int) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._sb.is_running():
                return
            time.sleep(_SANDBOX_READY_POLL_INTERVAL)
        raise DockerfileBuildError(
            "Sandbox did not become ready before dispatching startup command "
            f"within {timeout}s",
            instruction="CMD",
        )

    def launch_start_cmd(
        self,
        start_cmd: tuple[str, ...],
        envs: dict[str, str],
        workdir: str,
        user: str | None,
    ) -> CommandHandle:
        # ``start_cmd`` is normalized executable argv by resolve_start_cmd.
        wrapped = wrap_user(shlex.join(start_cmd), user)
        return self._sb.commands.run(
            wrapped,
            background=True,
            envs=envs or None,
            cwd=workdir,
        )


def _join_posix(base: str, rel: str) -> str:
    if not base.endswith("/"):
        base = base + "/"
    return base + rel.lstrip("/")


def wrap_user(command: str, user: str | None) -> str:
    """Wrap ``command`` for a supported named user.

    The parser already rejects unsupported USER syntax. This defensive check
    keeps direct callers from silently changing a ``user:group`` or numeric
    value into a different command identity.
    """
    if user is None:
        return command
    if user == "root":
        return command
    if not _is_named_user(user):
        raise ValueError(
            "USER must be a literal named user without a group or numeric ID"
        )
    return (
        f"if command -v runuser >/dev/null 2>&1; then "
        f"runuser -u {_shq(user)} -- sh -c {_shq(command)}; "
        f"else su -s /bin/sh {_shq(user)} -c {_shq(command)}; fi"
    )


def _is_named_user(value: str) -> bool:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_"
    characters = alphabet + "0123456789.-"
    return bool(value) and value[0] in alphabet and all(
        character in characters for character in value
    )


def _shq(s: str) -> str:
    """Single-quote a shell string."""
    return "'" + s.replace("'", "'\\''") + "'"


def _resolve_start_cmd(parsed: ParsedDockerfile) -> tuple[str, ...] | None:
    """Resolve the effective command through the shared OCI resolver.

    Shell forms are normalized to explicit ``/bin/sh -c`` argv.
    """
    return resolve_start_cmd(parsed.instructions)
