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

"""Dockerfile + build context source abstraction.

The Dockerfile sandbox-launch path parses a Dockerfile and executes its
build-time instructions inside an AKernel sandbox. A DockerContext abstracts
where the Dockerfile text and the context files come from. Local directories
are the built-in implementation (LocalDockerContext); subclasses can load from
OSS, S3, memory, etc.
"""

from __future__ import annotations

import fnmatch
import os
import posixpath
import stat
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal

from pathspec import GitIgnoreSpec

_SECURE_OPEN_SUPPORTED = (
    os.open in getattr(os, "supports_dir_fd", ())
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
)


class DockerContextError(ValueError):
    """Raised when a Docker build context cannot be safely selected."""


@dataclass(frozen=True)
class _SelectedContextFile:
    """A source context file and its relative destination target."""

    source_path: str
    relative_target: str


@dataclass(frozen=True)
class _ContextSelection:
    """A deterministic file selection for one COPY or ADD source."""

    source: str
    kind: Literal["literal_file", "literal_directory", "dot", "wildcard"]
    files: tuple[_SelectedContextFile, ...]


@dataclass(frozen=True)
class _ContextManifest:
    """Validated, ignored-filtered context entries used to plan selections."""

    _raw_files: tuple[str, ...]
    _visible_files: tuple[str, ...]
    _raw_directories: frozenset[str]

    @classmethod
    def from_context(cls, context: DockerContext) -> _ContextManifest:
        """Build a manifest without opening selectable context files."""

        try:
            raw_files = tuple(context.walk())
        except Exception as error:
            raise DockerContextError("failed to walk Docker context") from error

        _validate_walk_paths(raw_files)
        raw_files = tuple(sorted(raw_files))
        raw_directories = _directories_from_files(raw_files)
        ignore_spec = _read_ignore_spec(context, raw_files)

        reserved = {path for path in raw_files if _is_reserved_root_path(path)}
        visible_files = tuple(
            path
            for path in raw_files
            if path not in reserved and not ignore_spec.match_file(path)
        )
        return cls(raw_files, visible_files, frozenset(raw_directories))

    def select(self, source: str) -> _ContextSelection:
        """Plan one normalized Docker COPY or ADD source without opening files."""

        normalized = _normalize_source(source)
        if normalized == ".":
            if not self._visible_files:
                reason = "ignored" if self._raw_files else "no match"
                raise DockerContextError(f"context source is {reason}: {normalized!r}")
            return _ContextSelection(
                source=normalized,
                kind="dot",
                files=tuple(
                    _SelectedContextFile(path, path) for path in self._visible_files
                ),
            )
        if _has_wildcard(normalized):
            return self._select_wildcard(normalized)
        return self._select_literal(normalized)

    def _select_literal(self, source: str) -> _ContextSelection:
        if source in self._raw_files:
            if source not in self._visible_files:
                raise DockerContextError(f"context source is ignored: {source!r}")
            return _ContextSelection(
                source=source,
                kind="literal_file",
                files=(_SelectedContextFile(source, posixpath.basename(source)),),
            )

        if source in self._raw_directories:
            files = tuple(
                _SelectedContextFile(path, path.removeprefix(f"{source}/"))
                for path in self._visible_files
                if path.startswith(f"{source}/")
            )
            if not files:
                raise DockerContextError(f"context source is ignored: {source!r}")
            return _ContextSelection(
                source=source,
                kind="literal_directory",
                files=_validate_selection_files(files),
            )

        raise DockerContextError(f"context source has no match: {source!r}")

    def _select_wildcard(self, source: str) -> _ContextSelection:
        matched_files = [
            path for path in self._raw_files if _glob_matches(source, path)
        ]
        matched_directories = [
            path for path in self._raw_directories if _glob_matches(source, path)
        ]
        if not matched_files and not matched_directories:
            raise DockerContextError(f"context source has no match: {source!r}")

        top_directories: list[str] = []
        for directory in sorted(
            matched_directories, key=lambda item: (item.count("/"), item)
        ):
            if not any(
                directory.startswith(f"{parent}/") for parent in top_directories
            ):
                top_directories.append(directory)

        files: list[_SelectedContextFile] = []
        for directory in top_directories:
            prefix = f"{directory}/"
            for path in self._visible_files:
                if path.startswith(prefix):
                    child = path.removeprefix(prefix)
                    files.append(
                        _SelectedContextFile(
                            path, f"{posixpath.basename(directory)}/{child}"
                        )
                    )

        for path in self._visible_files:
            if not _glob_matches(source, path):
                continue
            if any(path.startswith(f"{directory}/") for directory in top_directories):
                continue
            files.append(_SelectedContextFile(path, posixpath.basename(path)))

        if not files:
            raise DockerContextError(f"context source is ignored: {source!r}")
        return _ContextSelection(
            source=source,
            kind="wildcard",
            files=_validate_selection_files(files),
        )


class DockerContext(ABC):
    """Dockerfile + build context files source abstraction.

    Local directories are the built-in implementation; subclasses can load from
    OSS, S3, memory, etc. File access is uniformly exposed as an open() stream
    so the runner can materialize and upload regardless of source.
    """

    @abstractmethod
    def dockerfile_text(self) -> str:
        """Return the Dockerfile content."""

    @abstractmethod
    @contextmanager
    def open(self, path: str) -> Iterator[BinaryIO]:
        """Open a file by its relative POSIX path inside the context.

        Yields a binary stream. The path is relative to the context root and
        uses POSIX separators regardless of host OS.
        """

    @abstractmethod
    def walk(self) -> Iterator[str]:
        """Enumerate relative POSIX paths of all context files.

        The Dockerfile itself is not part of the enumerated context files.
        """


def _to_posix(rel: str) -> str:
    """Normalize a relative path to POSIX separators."""

    return rel.replace(os.sep, "/").lstrip("/")


def _validate_walk_paths(paths: tuple[object, ...]) -> None:
    """Reject malformed context paths before they can affect selection."""

    seen: set[str] = set()
    for path in paths:
        if not isinstance(path, str):
            raise DockerContextError("context walk yielded a non-string path")
        if not path:
            raise DockerContextError("context walk yielded an empty path")
        if "\0" in path:
            raise DockerContextError("context walk yielded a path with NUL")
        if "\\" in path:
            raise DockerContextError("context walk yielded a path with backslash")
        if posixpath.isabs(path):
            raise DockerContextError("context walk yielded an absolute path")
        if path.endswith("/"):
            raise DockerContextError("context walk yielded a directory path")
        segments = path.split("/")
        if any(segment in ("", ".", "..") for segment in segments):
            raise DockerContextError("context walk yielded an unsafe path")
        if posixpath.normpath(path) != path:
            raise DockerContextError("context walk yielded a non-normalized path")
        if path in seen:
            raise DockerContextError(f"context walk yielded a duplicate path: {path!r}")
        seen.add(path)


def _directories_from_files(paths: tuple[str, ...]) -> set[str]:
    """Derive non-empty directories from walked file paths."""

    directories: set[str] = set()
    for path in paths:
        segments = path.split("/")
        for index in range(1, len(segments)):
            directories.add("/".join(segments[:index]))
    return directories


def _is_reserved_root_path(path: str) -> bool:
    """Return whether a root metadata file is never selectable."""

    return path.lower() == "dockerfile" or path == ".dockerignore"


def _read_ignore_spec(
    context: DockerContext, raw_files: tuple[str, ...]
) -> GitIgnoreSpec:
    """Read and compile the root .dockerignore when it was walked."""

    if ".dockerignore" not in raw_files:
        return GitIgnoreSpec.from_lines(())

    try:
        with context.open(".dockerignore") as stream:
            content = stream.read()
        lines = _prepare_ignore_lines(content.decode("utf-8-sig"))
        return GitIgnoreSpec.from_lines(lines)
    except DockerContextError:
        raise
    except Exception as error:
        raise DockerContextError("failed to read .dockerignore") from error


def _prepare_ignore_lines(content: str) -> list[str]:
    """Normalize common Docker .dockerignore syntax for GitIgnoreSpec."""

    lines: list[str] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line == ".":
            continue
        if line.startswith("#"):
            continue

        negation = ""
        pattern = line
        if pattern.startswith("!"):
            negation = "!"
            pattern = pattern[1:]

        while pattern.startswith("/"):
            pattern = pattern[1:]
        while pattern.endswith("/") and not _is_escaped(pattern, len(pattern) - 1):
            pattern = pattern[:-1]

        if not pattern or pattern == ".":
            continue
        lines.append(f"{negation}{pattern}")
    return lines


def _is_escaped(value: str, index: int) -> bool:
    """Return whether a character is preceded by an odd number of backslashes."""

    count = 0
    index -= 1
    while index >= 0 and value[index] == "\\":
        count += 1
        index -= 1
    return count % 2 == 1


def _normalize_source(source: str) -> str:
    """Normalize a Docker COPY or ADD source without permitting traversal."""

    if not isinstance(source, str):
        raise DockerContextError("context source must be a string")
    if not source:
        raise DockerContextError("context source is empty")
    if "\0" in source:
        raise DockerContextError("context source contains NUL")
    if "\\" in source:
        raise DockerContextError("context source contains backslash")
    if posixpath.isabs(source):
        raise DockerContextError("context source is absolute")
    if any(segment == ".." for segment in source.split("/")):
        raise DockerContextError("context source contains '..'")

    segments = [segment for segment in source.split("/") if segment not in ("", ".")]
    normalized = "/".join(segments)
    if not normalized:
        if all(segment in ("", ".") for segment in source.split("/")):
            return "."
        raise DockerContextError("context source is empty")
    return normalized


def _has_wildcard(path: str) -> bool:
    """Return whether a path contains a POSIX glob metacharacter."""

    return any(character in path for character in "*?[")


def _glob_matches(pattern: str, path: str) -> bool:
    """Match a POSIX path where ** spans zero or more path segments."""

    pattern_segments = pattern.split("/")
    path_segments = path.split("/")
    cache: dict[tuple[int, int], bool] = {}

    def match(pattern_index: int, path_index: int) -> bool:
        key = (pattern_index, path_index)
        if key in cache:
            return cache[key]
        if pattern_index == len(pattern_segments):
            result = path_index == len(path_segments)
        elif pattern_segments[pattern_index] == "**":
            result = any(
                match(pattern_index + 1, next_index)
                for next_index in range(path_index, len(path_segments) + 1)
            )
        elif path_index == len(path_segments):
            result = False
        else:
            result = fnmatch.fnmatchcase(
                path_segments[path_index], pattern_segments[pattern_index]
            ) and match(pattern_index + 1, path_index + 1)
        cache[key] = result
        return result

    return match(0, 0)


def _validate_selection_files(
    files: tuple[_SelectedContextFile, ...] | list[_SelectedContextFile],
) -> tuple[_SelectedContextFile, ...]:
    """Sort selection files and reject source or target collisions."""

    ordered = tuple(sorted(files, key=lambda item: item.source_path))
    source_paths = [item.source_path for item in ordered]
    target_paths = [item.relative_target for item in ordered]
    if len(source_paths) != len(set(source_paths)):
        raise DockerContextError("context selection has duplicate source paths")
    if len(target_paths) != len(set(target_paths)):
        raise DockerContextError("context selection has colliding target paths")
    return ordered


class LocalDockerContext(DockerContext):
    """Local filesystem backed DockerContext.

    dockerfile may be either an existing path to a Dockerfile or the Dockerfile
    content as a string (so callers can build a context purely in memory).
    """

    def __init__(
        self,
        dockerfile: str | Path,
        context_dir: str | Path | None = None,
    ) -> None:
        dockerfile_str = str(dockerfile)
        # If the argument points to an existing file, treat it as a path;
        # otherwise treat it as Dockerfile content directly.
        if os.path.isfile(dockerfile_str):
            with open(dockerfile_str, encoding="utf-8") as handle:
                self._dockerfile_text = handle.read()
            self._dockerfile_path = os.path.abspath(dockerfile_str)
            self._context_dir = os.path.abspath(
                os.path.dirname(self._dockerfile_path)
                if context_dir is None
                else str(context_dir)
            )
        else:
            self._dockerfile_text = dockerfile_str
            self._dockerfile_path = ""
            self._context_dir = os.path.abspath(
                "." if context_dir is None else str(context_dir)
            )

    def dockerfile_text(self) -> str:
        return self._dockerfile_text

    @contextmanager
    def open(self, path: str) -> Iterator[BinaryIO]:
        """Open a regular context file without following symbolic links.

        Every path component is opened relative to an already-open directory
        descriptor. This keeps validation and opening atomic with respect to
        symlink replacement.
        """

        normalized = _normalize_source(path)
        if normalized == ".":
            raise DockerContextError("context file path must name a file")
        if not _SECURE_OPEN_SUPPORTED:
            raise DockerContextError(
                "secure context file opening is not supported on this platform"
            )

        common_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        directory_flags = common_flags | os.O_DIRECTORY | os.O_NOFOLLOW
        file_flags = common_flags | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
        directory_fds: list[int] = []
        final_fd: int | None = None
        handle: BinaryIO | None = None
        try:
            root_fd = os.open(self._context_dir, directory_flags)
            directory_fds.append(root_fd)
            parent_fd = root_fd
            segments = normalized.split("/")
            for segment in segments[:-1]:
                parent_fd = os.open(
                    segment,
                    directory_flags,
                    dir_fd=parent_fd,
                )
                directory_fds.append(parent_fd)
            final_fd = os.open(
                segments[-1],
                file_flags,
                dir_fd=parent_fd,
            )
            if not stat.S_ISREG(os.fstat(final_fd).st_mode):
                raise DockerContextError(
                    f"context path is not a regular file: {normalized!r}"
                )
            handle = os.fdopen(final_fd, "rb")
            final_fd = None
        except DockerContextError:
            raise
        except (OSError, TypeError, ValueError) as error:
            raise DockerContextError(
                f"cannot securely open context file: {normalized!r}"
            ) from error
        finally:
            if handle is None:
                if final_fd is not None:
                    os.close(final_fd)
                for descriptor in reversed(directory_fds):
                    os.close(descriptor)

        try:
            yield handle
        finally:
            try:
                handle.close()
            finally:
                for descriptor in reversed(directory_fds):
                    os.close(descriptor)

    def walk(self) -> Iterator[str]:
        if not os.path.isdir(self._context_dir):
            return
        dockerfile_abs = (
            os.path.normpath(self._dockerfile_path) if self._dockerfile_path else None
        )
        for root, dirs, files in os.walk(self._context_dir):
            for name in dirs:
                if os.path.islink(os.path.join(root, name)):
                    raise DockerContextError(
                        f"context contains a symbolic link: {name!r}"
                    )
            dirs.sort()
            for name in sorted(files):
                full = os.path.join(root, name)
                if os.path.islink(full):
                    raise DockerContextError(
                        f"context contains a symbolic link: {name!r}"
                    )
                if dockerfile_abs and os.path.normpath(full) == dockerfile_abs:
                    continue
                if (
                    not self._dockerfile_path
                    and root == os.path.normpath(self._context_dir)
                    and name.lower() == "dockerfile"
                ):
                    continue
                rel = os.path.relpath(full, self._context_dir)
                yield _to_posix(rel)

    @property
    def context_dir(self) -> str:
        """Absolute path to the local context root."""

        return self._context_dir
