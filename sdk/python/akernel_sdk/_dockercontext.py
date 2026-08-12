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
class DockerContextEntry:
    """One file or directory exposed by a :class:`DockerContext`.

    ``path`` is relative to the context root and uses POSIX separators.
    ``mode`` contains only permission bits, from ``0`` through ``0o777``.
    """

    path: str
    kind: Literal["file", "directory"]
    mode: int

    def __post_init__(self) -> None:
        if self.kind not in ("file", "directory"):
            raise ValueError("context entry kind must be 'file' or 'directory'")
        if (
            type(self.mode) is not int
            or self.mode < 0
            or self.mode > 0o777
        ):
            raise ValueError("context entry mode must contain only permission bits")


@dataclass(frozen=True)
class _SelectedContextEntry:
    """A source context entry and its relative destination target."""

    source_path: str
    relative_target: str
    kind: Literal["file", "directory"]
    mode: int


@dataclass(frozen=True)
class _ContextSelection:
    """A deterministic entry selection for one COPY or ADD source."""

    source: str
    kind: Literal["literal_file", "literal_directory", "dot", "wildcard"]
    entries: tuple[_SelectedContextEntry, ...]

    @property
    def has_directories(self) -> bool:
        """Return whether this selection contains an explicit directory."""

        return any(entry.kind == "directory" for entry in self.entries)


@dataclass(frozen=True)
class _ContextManifest:
    """Validated, ignored-filtered context entries used to plan selections."""

    _raw_entries: tuple[DockerContextEntry, ...]
    _visible_entries: tuple[DockerContextEntry, ...]

    @classmethod
    def from_context(cls, context: DockerContext) -> _ContextManifest:
        """Build a manifest without opening selectable context files."""

        try:
            raw_entries = tuple(context.walk())
        except Exception as error:
            raise DockerContextError("failed to walk Docker context") from error

        _validate_walk_entries(raw_entries)
        raw_entries = tuple(sorted(raw_entries, key=lambda entry: entry.path))
        raw_files = tuple(
            entry.path for entry in raw_entries if entry.kind == "file"
        )
        ignore_spec = _read_ignore_spec(context, raw_files)
        visible_entries = tuple(
            entry
            for entry in raw_entries
            if not _is_reserved_root_path(entry.path)
            and not ignore_spec.match_file(entry.path)
        )
        return cls(raw_entries, visible_entries)

    def select(self, source: str) -> _ContextSelection:
        """Plan one normalized Docker COPY or ADD source without opening files."""

        normalized = _normalize_source(source)
        if normalized == ".":
            if not self._visible_entries:
                reason = "ignored" if self._raw_entries else "no match"
                raise DockerContextError(f"context source is {reason}: {normalized!r}")
            return _ContextSelection(
                source=normalized,
                kind="dot",
                entries=tuple(
                    _selected_entry(entry, entry.path)
                    for entry in self._visible_entries
                ),
            )
        if _has_wildcard(normalized):
            return self._select_wildcard(normalized)
        return self._select_literal(normalized)

    def _select_literal(self, source: str) -> _ContextSelection:
        raw_by_path = {entry.path: entry for entry in self._raw_entries}
        visible_by_path = {entry.path: entry for entry in self._visible_entries}
        entry = raw_by_path.get(source)
        if entry is None:
            raise DockerContextError(f"context source has no match: {source!r}")
        if source not in visible_by_path:
            raise DockerContextError(f"context source is ignored: {source!r}")
        if entry.kind == "file":
            return _ContextSelection(
                source=source,
                kind="literal_file",
                entries=(_selected_entry(entry, posixpath.basename(source)),),
            )

        entries = tuple(
            _selected_entry(
                item,
                "" if item.path == source else item.path.removeprefix(f"{source}/"),
            )
            for item in self._visible_entries
            if item.path == source or item.path.startswith(f"{source}/")
        )
        if not entries:
            raise DockerContextError(f"context source is ignored: {source!r}")
        return _ContextSelection(
            source=source,
            kind="literal_directory",
            entries=_validate_selection_entries(entries),
        )

    def _select_wildcard(self, source: str) -> _ContextSelection:
        matched_entries = [
            entry for entry in self._raw_entries if _glob_matches(source, entry.path)
        ]
        if not matched_entries:
            raise DockerContextError(f"context source has no match: {source!r}")

        top_directories: list[str] = []
        for entry in sorted(
            (entry for entry in matched_entries if entry.kind == "directory"),
            key=lambda item: (item.path.count("/"), item.path),
        ):
            if not any(
                entry.path.startswith(f"{parent}/") for parent in top_directories
            ):
                top_directories.append(entry.path)

        entries: list[_SelectedContextEntry] = []
        for directory in top_directories:
            prefix = f"{directory}/"
            for entry in self._visible_entries:
                if entry.path == directory or entry.path.startswith(prefix):
                    child = (
                        ""
                        if entry.path == directory
                        else entry.path.removeprefix(prefix)
                    )
                    target = posixpath.basename(directory)
                    if child:
                        target = f"{target}/{child}"
                    entries.append(_selected_entry(entry, target))

        for entry in self._visible_entries:
            if not _glob_matches(source, entry.path):
                continue
            if any(
                entry.path == directory or entry.path.startswith(f"{directory}/")
                for directory in top_directories
            ):
                continue
            entries.append(_selected_entry(entry, posixpath.basename(entry.path)))

        if not entries:
            raise DockerContextError(f"context source is ignored: {source!r}")
        return _ContextSelection(
            source=source,
            kind="wildcard",
            entries=_validate_selection_entries(entries),
        )


def _selected_entry(
    entry: DockerContextEntry, relative_target: str
) -> _SelectedContextEntry:
    """Attach a destination-relative path to a context entry."""

    return _SelectedContextEntry(
        source_path=entry.path,
        relative_target=relative_target,
        kind=entry.kind,
        mode=entry.mode,
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
    def walk(self) -> Iterator[DockerContextEntry]:
        """Enumerate every context file and directory as structured entries.

        Entries use relative POSIX paths. The Dockerfile itself is not included.
        File entries must be readable through :meth:`open`; directory entries
        make empty directories and their permission modes representable.
        """


def _to_posix(rel: str) -> str:
    """Normalize a relative path to POSIX separators."""

    return rel.replace(os.sep, "/").lstrip("/")


def _validate_walk_entries(entries: tuple[object, ...]) -> None:
    """Reject malformed context entries before they can affect selection."""

    seen: dict[str, DockerContextEntry] = {}
    for entry in entries:
        if not isinstance(entry, DockerContextEntry):
            raise DockerContextError("context walk yielded a non-entry")
        path = entry.path
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
        seen[path] = entry

    for path in seen:
        parent = posixpath.dirname(path)
        while parent not in ("", "."):
            ancestor = seen.get(parent)
            if ancestor is None:
                raise DockerContextError(
                    f"context walk omitted directory entry: {parent!r}"
                )
            if ancestor.kind == "file":
                raise DockerContextError(
                    f"context walk yielded a file-as-ancestor path: {parent!r}"
                )
            parent = posixpath.dirname(parent)

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


def _validate_selection_entries(
    entries: tuple[_SelectedContextEntry, ...] | list[_SelectedContextEntry],
) -> tuple[_SelectedContextEntry, ...]:
    """Sort selection entries and reject source or target collisions."""

    ordered = tuple(sorted(entries, key=lambda item: item.source_path))
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

    def walk(self) -> Iterator[DockerContextEntry]:
        """Deterministically enumerate regular files and all directories.

        Symlinks and non-regular files fail closed. Keeping directory entries is
        required to preserve empty directories and their modes during COPY.
        """

        if not os.path.isdir(self._context_dir):
            return
        dockerfile_abs = (
            os.path.normpath(self._dockerfile_path) if self._dockerfile_path else None
        )
        entries: list[DockerContextEntry] = []
        for root, dirs, files in os.walk(self._context_dir):
            dirs.sort()
            for name in dirs:
                full = os.path.join(root, name)
                info = os.lstat(full)
                if stat.S_ISLNK(info.st_mode):
                    raise DockerContextError(
                        f"context contains a symbolic link: {name!r}"
                    )
                if not stat.S_ISDIR(info.st_mode):
                    raise DockerContextError(
                        f"context path is not a directory: {name!r}"
                    )
                rel = _to_posix(os.path.relpath(full, self._context_dir))
                entries.append(
                    DockerContextEntry(rel, "directory", stat.S_IMODE(info.st_mode))
                )
            for name in sorted(files):
                full = os.path.join(root, name)
                info = os.lstat(full)
                if stat.S_ISLNK(info.st_mode):
                    raise DockerContextError(
                        f"context contains a symbolic link: {name!r}"
                    )
                if not stat.S_ISREG(info.st_mode):
                    raise DockerContextError(
                        f"context path is not a regular file: {name!r}"
                    )
                if dockerfile_abs and os.path.normpath(full) == dockerfile_abs:
                    continue
                if (
                    not self._dockerfile_path
                    and root == os.path.normpath(self._context_dir)
                    and name.lower() == "dockerfile"
                ):
                    continue
                rel = _to_posix(os.path.relpath(full, self._context_dir))
                entries.append(
                    DockerContextEntry(rel, "file", stat.S_IMODE(info.st_mode))
                )
        yield from sorted(entries, key=lambda entry: entry.path)

    @property
    def context_dir(self) -> str:
        """Absolute path to the local context root."""

        return self._context_dir
