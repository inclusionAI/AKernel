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

"""Dockerfile parsing for the sandbox-launch path.

Line-level parsing is delegated to ``dockerfile-parse`` (BSD-3-Clause): it
handles comments, instruction case-insensitivity, line continuations and the
``# escape=`` directive. This module performs value-level post-processing and
translates the deliberately narrow direct-launch subset to typed structures.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass, field

from ._dockercontext import DockerContext
from ._dockerfile_launch import DockerfileLaunch  # noqa: F401


@dataclass(frozen=True)
class FromInstruction:
    image: str


@dataclass(frozen=True)
class RunInstruction:
    command: str


@dataclass(frozen=True)
class CopyInstruction:
    srcs: tuple[str, ...]
    dest: str
    chown: str | None = None
    is_add: bool = False


@dataclass(frozen=True)
class EnvInstruction:
    envs: dict[str, str]


@dataclass(frozen=True)
class WorkdirInstruction:
    path: str


@dataclass(frozen=True)
class UserInstruction:
    user: str


@dataclass(frozen=True)
class CmdInstruction:
    cmd: tuple[str, ...]
    shell_form: bool


@dataclass(frozen=True)
class EntrypointInstruction:
    cmd: tuple[str, ...]
    shell_form: bool


@dataclass(frozen=True)
class ExposeInstruction:
    ports: tuple[str, ...]


@dataclass(frozen=True)
class UnsupportedInstruction:
    """Syntax the sandbox-launch path deliberately does not execute."""

    kind: str
    value: str
    reason: str = "unsupported_instruction"


BuildInstruction = (
    FromInstruction
    | RunInstruction
    | CopyInstruction
    | EnvInstruction
    | WorkdirInstruction
    | UserInstruction
    | CmdInstruction
    | EntrypointInstruction
    | ExposeInstruction
)


@dataclass(frozen=True)
class ParsedDockerfile:
    base_image: str
    instructions: tuple[BuildInstruction, ...]
    unsupported: tuple[UnsupportedInstruction, ...]
    envs: dict[str, str] = field(default_factory=dict)
    workdir: str = "/"
    user: str | None = None
    start_cmd: tuple[str, ...] | None = None
    entrypoint: tuple[str, ...] | None = None
    exposed_ports: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def _last_start_instructions(
    instructions: Sequence[BuildInstruction],
) -> tuple[EntrypointInstruction | None, CmdInstruction | None]:
    """Return the last declared ENTRYPOINT and CMD instructions."""
    entrypoint: EntrypointInstruction | None = None
    cmd: CmdInstruction | None = None
    for instruction in instructions:
        if isinstance(instruction, EntrypointInstruction):
            entrypoint = instruction
        elif isinstance(instruction, CmdInstruction):
            cmd = instruction
    return entrypoint, cmd


def _as_argv(cmd: tuple[str, ...], *, shell_form: bool) -> tuple[str, ...]:
    """Normalize a parsed command to argv without inferring from its text."""
    return ("/bin/sh", "-c", cmd[0]) if shell_form else cmd


def resolve_start_cmd(
    instructions: Sequence[BuildInstruction],
) -> tuple[str, ...] | None:
    """Resolve the effective command to executable argv per OCI semantics."""
    entrypoint, cmd = _last_start_instructions(instructions)
    if entrypoint is not None:
        if entrypoint.shell_form:
            return _as_argv(entrypoint.cmd, shell_form=True)
        if cmd is None:
            return entrypoint.cmd
        return entrypoint.cmd + _as_argv(cmd.cmd, shell_form=cmd.shell_form)
    return _as_argv(cmd.cmd, shell_form=cmd.shell_form) if cmd is not None else None


def _resolve_entrypoint(
    instructions: Sequence[BuildInstruction],
) -> tuple[str, ...] | None:
    """Return the last declared ENTRYPOINT as executable argv."""
    entrypoint, _ = _last_start_instructions(instructions)
    if entrypoint is None:
        return None
    return _as_argv(entrypoint.cmd, shell_form=entrypoint.shell_form)


_IGNORED_INSTRUCTIONS: dict[str, str] = {
    "VOLUME": "not supported; use storage_mb or mounts for persistence",
    "LABEL": "not supported",
    "HEALTHCHECK": "not supported",
    "SHELL": "not supported",
    "STOPSIGNAL": "not supported",
    "ONBUILD": "not supported",
    "MAINTAINER": "not supported (deprecated)",
}

_CHOWN_VALUE_RE = re.compile(
    r"[A-Za-z0-9_][A-Za-z0-9_.-]*(?::[A-Za-z0-9_][A-Za-z0-9_.-]*)?"
)
_USER_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*\Z")
_VARIABLE_RE = re.compile(r"\$(?:\{[^}]*\}|[A-Za-z_][A-Za-z0-9_]*)")
DIRECT_LAUNCH_ROOTFS_ONLY_WARNING = (
    "FROM supplies only the root filesystem; inherited image ENV, USER, WORKDIR, "
    "CMD and ENTRYPOINT are not applied. Declare required runtime settings in this "
    "Dockerfile or pre-build and use Sandbox(image=...)."
)


class DockerfileParseError(ValueError):
    """Raised when a Dockerfile cannot be parsed for direct sandbox launch."""

    def __init__(self, message: str, *, reason: str = "unsupported_syntax") -> None:
        super().__init__(message)
        self.reason = reason


class DockerfileBuildError(RuntimeError):
    """Raised when a build-time instruction fails inside the sandbox."""

    def __init__(
        self,
        message: str,
        *,
        index: int | None = None,
        instruction: str | None = None,
        stdout: str = "",
        stderr: str = "",
        exit_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.index = index
        self.instruction = instruction
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code


@dataclass(frozen=True)
class DockerfileCheckResult:
    """Result of :func:`check_direct_launch`."""

    direct_launchable: bool
    base_image: str | None
    reasons: tuple[str, ...]
    has_build_instructions: bool
    ignored_instructions: tuple[str, ...]
    warnings: tuple[str, ...]


def parse_dockerfile(
    context: DockerContext, *, strict: bool = False
) -> ParsedDockerfile:
    """Parse a Dockerfile into a :class:`ParsedDockerfile`.

    ``strict=True`` raises at the first unsupported instruction or syntax.
    Non-strict parsing is diagnostic only: unsupported input is recorded and is
    never translated into an executable instruction.
    """
    structure = _load_structure(context.dockerfile_text())
    froms = [node for node in structure if node["instruction"] == "FROM"]
    if not froms:
        raise DockerfileParseError(
            "Dockerfile must contain a FROM instruction", reason="no_from"
        )
    if len(froms) > 1:
        raise DockerfileParseError(
            "Multi-stage Dockerfiles are not supported; pre-build the image "
            "and launch with Sandbox(image=...)",
            reason="multi_stage",
        )

    unsupported: list[UnsupportedInstruction] = []
    warnings: list[str] = []
    base_image = _parse_from(_value(froms[0]), strict, unsupported, warnings)
    instructions: list[BuildInstruction] = [FromInstruction(image=base_image)]
    envs: dict[str, str] = {}
    workdir = "/"
    user: str | None = None
    exposed_ports: list[str] = []

    for node in structure:
        kind = node["instruction"]
        value = _value(node)
        if kind in ("COMMENT", "FROM"):
            continue
        if kind == "RUN":
            if _json_array(value) is not None:
                _unsupported(
                    kind,
                    value,
                    "exec-form RUN is not supported; use shell-form RUN",
                    "unsupported_syntax",
                    strict,
                    unsupported,
                    warnings,
                )
                continue
            cmd = _fold_run(value)
            if not cmd:
                continue
            args = _split_shell_like(value, kind, strict, unsupported, warnings)
            if args is None:
                continue
            if args and args[0].startswith("--"):
                _unsupported(
                    kind,
                    value,
                    "RUN flags are not supported",
                    "unsupported_syntax",
                    strict,
                    unsupported,
                    warnings,
                )
                continue
            instructions.append(RunInstruction(command=cmd))
        elif kind in ("COPY", "ADD"):
            _parse_copy_add(value, kind, strict, unsupported, warnings, instructions)
        elif kind == "ENV":
            parsed = _parse_env(value, strict, unsupported, warnings)
            if parsed is not None:
                envs.update(parsed)
                instructions.append(EnvInstruction(envs=dict(parsed)))
        elif kind == "ARG":
            _unsupported(
                kind,
                value,
                "ARG is not supported; pre-build the image and launch with "
                "Sandbox(image=...)",
                "unsupported_instruction",
                strict,
                unsupported,
                warnings,
            )
        elif kind == "WORKDIR":
            wd = value.strip()
            if not wd or not wd.startswith("/"):
                _unsupported(
                    kind,
                    value,
                    "WORKDIR must be an absolute path",
                    "unsupported_syntax",
                    strict,
                    unsupported,
                    warnings,
                )
            elif _contains_variable(wd):
                _unsupported(
                    kind,
                    value,
                    "WORKDIR variable expansion is not supported",
                    "unsupported_syntax",
                    strict,
                    unsupported,
                    warnings,
                )
            else:
                workdir = wd
                instructions.append(WorkdirInstruction(path=wd))
        elif kind == "USER":
            user_value = value.strip()
            if _contains_variable(user_value):
                _unsupported(
                    kind,
                    value,
                    "USER variable expansion is not supported",
                    "unsupported_syntax",
                    strict,
                    unsupported,
                    warnings,
                )
            elif not _is_named_user(user_value):
                _unsupported(
                    kind,
                    value,
                    "USER must be a literal named user without a group or numeric ID",
                    "unsupported_syntax",
                    strict,
                    unsupported,
                    warnings,
                )
            else:
                user = user_value
                instructions.append(UserInstruction(user=user_value))
        elif kind in ("CMD", "ENTRYPOINT"):
            _parse_cmd_entrypoint(
                value, kind, strict, unsupported, warnings, instructions
            )
        elif kind == "EXPOSE":
            ports = tuple(port for port in value.split() if port)
            if ports:
                exposed_ports.extend(ports)
                instructions.append(ExposeInstruction(ports=ports))
        elif kind in _IGNORED_INSTRUCTIONS:
            _unsupported(
                kind,
                value,
                f"{kind} {_IGNORED_INSTRUCTIONS[kind]}",
                "unsupported_instruction",
                strict,
                unsupported,
                warnings,
            )
        else:
            _unsupported(
                kind,
                value,
                f"{kind} is not supported",
                "unsupported_instruction",
                strict,
                unsupported,
                warnings,
            )

    return ParsedDockerfile(
        base_image=base_image,
        instructions=tuple(instructions),
        unsupported=tuple(unsupported),
        envs=envs,
        workdir=workdir,
        user=user,
        start_cmd=resolve_start_cmd(instructions),
        entrypoint=_resolve_entrypoint(instructions),
        exposed_ports=tuple(exposed_ports),
        warnings=tuple(warnings),
    )


def check_direct_launch(
    context: DockerContext, *, strict: bool = False
) -> DockerfileCheckResult:
    """Check whether ``Sandbox(dockerfile=DockerfileLaunch(...))`` can
    execute a Dockerfile safely.
    """
    try:
        parsed = parse_dockerfile(context, strict=strict)
    except DockerfileParseError as exc:
        return DockerfileCheckResult(
            direct_launchable=False,
            base_image=None,
            reasons=(exc.reason,),
            has_build_instructions=False,
            ignored_instructions=(),
            warnings=(str(exc),),
        )

    has_build = any(
        isinstance(instruction, (RunInstruction, CopyInstruction))
        for instruction in parsed.instructions
    )
    unsupported_kinds = tuple(sorted({item.kind for item in parsed.unsupported}))
    reasons = tuple(dict.fromkeys(item.reason for item in parsed.unsupported))
    warnings = list(parsed.warnings)
    if has_build and not parsed.unsupported:
        warnings.append(
            "Dockerfile contains RUN/COPY/ADD; "
            "Sandbox(dockerfile=DockerfileLaunch(...)) re-runs them on every "
            "launch (no snapshot). For build-once reuse, "
            "pre-build the image and launch with Sandbox(image=...)."
        )
    if not parsed.unsupported:
        warnings.append(DIRECT_LAUNCH_ROOTFS_ONLY_WARNING)

    return DockerfileCheckResult(
        direct_launchable=not parsed.unsupported,
        base_image=parsed.base_image if not parsed.unsupported else None,
        reasons=reasons,
        has_build_instructions=has_build,
        ignored_instructions=unsupported_kinds,
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# Value post-processing helpers
# ---------------------------------------------------------------------------


def _load_structure(text: str) -> Sequence[dict]:
    """Load a Dockerfile's instruction structure via dockerfile-parse."""
    import os
    import tempfile

    from dockerfile_parse import DockerfileParser  # type: ignore[import-untyped]

    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "Dockerfile"), "w", encoding="utf-8") as fh:
            fh.write(text)
        parser = DockerfileParser(path=tmp)
        return list(parser.structure)


def _value(node: dict) -> str:
    return str(node.get("value", ""))


def _parse_from(
    value: str,
    strict: bool,
    unsupported: list[UnsupportedInstruction],
    warnings: list[str],
) -> str:
    args = _split_shell_like(value, "FROM", strict, unsupported, warnings)
    if args is None:
        return ""
    if not args:
        _unsupported(
            "FROM",
            value,
            "FROM requires an image",
            "unsupported_syntax",
            strict,
            unsupported,
            warnings,
        )
        return ""
    if args[0].startswith("--"):
        _unsupported(
            "FROM",
            value,
            "FROM flags are not supported",
            "unsupported_syntax",
            strict,
            unsupported,
            warnings,
        )
        return _first_non_flag(args)
    image = args[0]
    if _contains_variable(image):
        _unsupported(
            "FROM",
            value,
            "FROM variable expansion is not supported",
            "unsupported_syntax",
            strict,
            unsupported,
            warnings,
        )
    elif len(args) not in (1, 3) or (len(args) == 3 and args[1].lower() != "as"):
        _unsupported(
            "FROM",
            value,
            "FROM supports only 'FROM image AS alias'",
            "unsupported_syntax",
            strict,
            unsupported,
            warnings,
        )
    return image


def _first_non_flag(args: list[str]) -> str:
    for arg in args:
        if not arg.startswith("--"):
            return arg
    return ""


def _json_array(value: str) -> list[object] | None:
    """Return ``value`` as a JSON array, or ``None`` for shell-form text."""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


def _fold_run(value: str) -> str:
    if not value.strip():
        return ""
    return re.sub(r"\\\s*\n\s*", " ", value).strip()


def _split_shell_like(
    value: str,
    kind: str,
    strict: bool,
    unsupported: list[UnsupportedInstruction],
    warnings: list[str],
) -> list[str] | None:
    try:
        return shlex.split(value, posix=True)
    except ValueError:
        _unsupported(
            kind,
            value,
            f"{kind} contains malformed quoting",
            "unsupported_syntax",
            strict,
            unsupported,
            warnings,
        )
        return None


def _parse_copy_add(
    value: str,
    kind: str,
    strict: bool,
    unsupported: list[UnsupportedInstruction],
    warnings: list[str],
    out: list[BuildInstruction],
) -> None:
    if not value.strip():
        _unsupported(
            kind,
            value,
            f"{kind} requires a source and a destination",
            "unsupported_syntax",
            strict,
            unsupported,
            warnings,
        )
        return
    if _json_array(value) is not None:
        _unsupported(
            kind,
            value,
            f"JSON-form {kind} is not supported",
            "unsupported_syntax",
            strict,
            unsupported,
            warnings,
        )
        return
    args = _split_shell_like(value, kind, strict, unsupported, warnings)
    if args is None:
        return

    chown: str | None = None
    paths: list[str] = []
    for arg in args:
        if arg.startswith("--from"):
            _unsupported(
                kind,
                value,
                "COPY --from is not supported (multi-stage); pre-build "
                "the image and launch with Sandbox(image=...)",
                "multi_stage",
                strict,
                unsupported,
                warnings,
            )
            return
        if arg.startswith("--chown="):
            chown = arg.split("=", 1)[1]
            if not _CHOWN_VALUE_RE.fullmatch(chown) or _contains_variable(chown):
                _unsupported(
                    kind,
                    value,
                    "--chown must be a literal user[:group]",
                    "unsupported_syntax",
                    strict,
                    unsupported,
                    warnings,
                )
                return
            continue
        if arg.startswith("--"):
            _unsupported(
                kind,
                value,
                f"{kind} flag {arg!r} is not supported",
                "unsupported_syntax",
                strict,
                unsupported,
                warnings,
            )
            return
        paths.append(arg)

    if len(paths) < 2:
        _unsupported(
            kind,
            value,
            f"{kind} requires at least a source and a destination",
            "unsupported_syntax",
            strict,
            unsupported,
            warnings,
        )
        return
    if any(_contains_variable(path) for path in paths):
        _unsupported(
            kind,
            value,
            f"{kind} path variable expansion is not supported",
            "unsupported_syntax",
            strict,
            unsupported,
            warnings,
        )
        return
    if kind == "ADD" and any(_is_remote_url(path) for path in paths[:-1]):
        _unsupported(
            kind,
            value,
            "ADD remote URLs are not supported; download into the "
            "build context first or pre-build and use Sandbox(image=...)",
            "remote_add",
            strict,
            unsupported,
            warnings,
        )
        return
    srcs = tuple(paths[:-1])
    dest = paths[-1]
    if len(srcs) > 1 and not dest.endswith("/"):
        _unsupported(
            kind,
            value,
            f"{kind} destination must be a directory ending in '/' for "
            "multiple sources",
            "unsupported_syntax",
            strict,
            unsupported,
            warnings,
        )
        return
    out.append(
        CopyInstruction(
            srcs=srcs, dest=dest, chown=chown, is_add=(kind == "ADD")
        )
    )


def _parse_env(
    value: str,
    strict: bool,
    unsupported: list[UnsupportedInstruction],
    warnings: list[str],
) -> dict[str, str] | None:
    value = value.strip()
    if not value:
        _unsupported(
            "ENV",
            value,
            "ENV requires a key and value",
            "unsupported_syntax",
            strict,
            unsupported,
            warnings,
        )
        return None
    try:
        tokens = shlex.split(value, posix=True)
    except ValueError:
        _unsupported(
            "ENV",
            value,
            "ENV contains malformed quoting",
            "unsupported_syntax",
            strict,
            unsupported,
            warnings,
        )
        return None
    if "=" in tokens[0]:
        if not all("=" in token for token in tokens):
            _unsupported(
                "ENV",
                value,
                "ENV assignment form is malformed",
                "unsupported_syntax",
                strict,
                unsupported,
                warnings,
            )
            return None
        result = dict(token.split("=", 1) for token in tokens)
    elif len(tokens) >= 2:
        result = {tokens[0]: " ".join(tokens[1:])}
    else:
        _unsupported(
            "ENV",
            value,
            "ENV requires a key and value",
            "unsupported_syntax",
            strict,
            unsupported,
            warnings,
        )
        return None
    if any(_contains_variable(item) for item in result.values()):
        _unsupported(
            "ENV",
            value,
            "ENV variable expansion is not supported",
            "unsupported_syntax",
            strict,
            unsupported,
            warnings,
        )
        return None
    return result


def _parse_cmd_entrypoint(
    value: str,
    kind: str,
    strict: bool,
    unsupported: list[UnsupportedInstruction],
    warnings: list[str],
    out: list[BuildInstruction],
) -> None:
    value = value.strip()
    if not value:
        _unsupported(
            kind,
            value,
            f"{kind} requires a command",
            "unsupported_syntax",
            strict,
            unsupported,
            warnings,
        )
        return
    parsed = _json_array(value)
    if parsed is not None:
        if not all(
            isinstance(item, str) for item in parsed
        ):
            _unsupported(
                kind,
                value,
                f"{kind} must be a JSON array of strings",
                "unsupported_syntax",
                strict,
                unsupported,
                warnings,
            )
            return
        cmd = tuple(item for item in parsed if isinstance(item, str))
        if not cmd or not cmd[0]:
            _unsupported(
                kind,
                value,
                f"{kind} requires a non-empty command name",
                "unsupported_syntax",
                strict,
                unsupported,
                warnings,
            )
            return
        if kind == "CMD":
            out.append(CmdInstruction(cmd=cmd, shell_form=False))
        else:
            out.append(EntrypointInstruction(cmd=cmd, shell_form=False))
        return
    folded = re.sub(r"\\\s*\n\s*", " ", value).strip()
    if kind == "CMD":
        out.append(CmdInstruction(cmd=(folded,), shell_form=True))
    else:
        out.append(EntrypointInstruction(cmd=(folded,), shell_form=True))


def _is_named_user(value: str) -> bool:
    """Return whether ``value`` is within the direct-launch USER subset."""

    return bool(_USER_NAME_RE.fullmatch(value))


def _contains_variable(value: str) -> bool:
    return bool(_VARIABLE_RE.search(value))


def _is_remote_url(value: str) -> bool:
    return value.lower().startswith(("http://", "https://"))


def _unsupported(
    kind: str,
    value: str,
    message: str,
    reason: str,
    strict: bool,
    unsupported: list[UnsupportedInstruction],
    warnings: list[str],
) -> None:
    if strict:
        raise DockerfileParseError(message, reason=reason)
    unsupported.append(UnsupportedInstruction(kind=kind, value=value, reason=reason))
    warnings.append(message)
