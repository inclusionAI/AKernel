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

import os
import posixpath
import stat
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal

_SECURE_OPEN_SUPPORTED = (
    os.open in getattr(os, "supports_dir_fd", ())
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
)


class DockerContextError(ValueError):
    """Raised when a Docker build context cannot be safely selected."""


@dataclass(frozen=True)
class _CharacterClass:
    """One prevalidated Go filepath-style character class."""

    negated: bool
    ranges: tuple[tuple[str, str], ...]

    def matches(self, character: str) -> bool:
        """Return whether a Unicode codepoint belongs to this class."""

        matched = any(low <= character <= high for low, high in self.ranges)
        return matched != self.negated


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
    top_level_source_count: int

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
        ignore_matcher = _read_ignore_matcher(context, raw_files)
        visible_entries = _visible_entries_with_ancestors(
            raw_entries, ignore_matcher
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
                top_level_source_count=1,
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
                top_level_source_count=1,
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
            top_level_source_count=1,
        )

    def _select_wildcard(self, source: str) -> _ContextSelection:
        pattern = _compile_source_pattern(source)
        matched_entries = [
            entry for entry in self._raw_entries if _glob_matches(pattern, entry.path)
        ]
        if not matched_entries:
            raise DockerContextError(f"context source has no match: {source!r}")

        top_directories: list[str] = []
        for entry in sorted(
            (
                entry
                for entry in self._visible_entries
                if entry.kind == "directory" and _glob_matches(pattern, entry.path)
            ),
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
                    target = (
                        ""
                        if entry.path == directory
                        else entry.path.removeprefix(prefix)
                    )
                    entries.append(_selected_entry(entry, target))

        top_level_files = [
            entry
            for entry in self._visible_entries
            if entry.kind == "file"
            and _glob_matches(pattern, entry.path)
            and not any(
                entry.path.startswith(f"{directory}/")
                for directory in top_directories
            )
        ]
        for entry in top_level_files:
            entries.append(_selected_entry(entry, posixpath.basename(entry.path)))

        if not entries:
            raise DockerContextError(f"context source is ignored: {source!r}")
        return _ContextSelection(
            source=source,
            kind="wildcard",
            entries=_validate_selection_entries(entries),
            top_level_source_count=len(top_directories) + len(top_level_files),
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

    def dockerfile_ignore(self) -> tuple[str, bytes] | None:
        """Return the selected Dockerfile-specific ignore file, if any.

        The returned name is used for diagnostics and the bytes are compiled as
        the active ignore matcher. Returning None lets the caller fall back to
        the root .dockerignore.
        """

        return None

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

        Entries use relative POSIX paths. Control files that belong to the
        filesystem context, including Dockerfiles and ignore files, must also
        be enumerated; their COPY visibility is determined by the active
        ignore matcher. File entries must be readable through :meth:`open`; directory
        entries make empty directories and their permission modes representable.
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


# The matcher below adapts moby/patternmatcher commit 5a6d8429a19b
# (Apache-2.0) to keep Docker context filtering backend-neutral.
@dataclass(frozen=True)
class _DockerIgnoreToken:
    """One token in a Moby-compatible ignore pattern."""

    kind: Literal[
        "literal",
        "star",
        "question",
        "globstar",
        "globstar_directory",
        "class",
        "anchor",
    ]
    value: str | _CharacterClass | None = None


@dataclass(frozen=True)
class _DockerIgnorePattern:
    """One precompiled Moby patternmatcher-compatible ignore pattern."""

    value: str
    exclusion: bool
    match_type: Literal["exact", "prefix", "suffix", "tokens"]
    tokens: tuple[_DockerIgnoreToken, ...] = ()

    def matches(self, path: str) -> bool:
        """Match one context path using Moby's optimized pattern forms."""

        if self.match_type == "exact":
            return path == self.value
        if self.match_type == "prefix":
            return path.startswith(self.value[:-2])
        if self.match_type == "suffix":
            suffix = self.value[2:]
            return path.endswith(suffix) or (
                suffix.startswith("/") and path == suffix[1:]
            )
        return _ignore_tokens_match(self.tokens, path)


@dataclass(frozen=True)
class _DockerIgnoreMatcher:
    """Ordered Docker ignore patterns with parent-directory matching."""

    patterns: tuple[_DockerIgnorePattern, ...]

    @classmethod
    def from_lines(cls, lines: list[str]) -> _DockerIgnoreMatcher:
        return cls(tuple(_compile_ignore_pattern(line) for line in lines))

    def is_ignored(self, path: str) -> bool:
        """Return Moby MatchesOrParentMatches result for a context path."""

        matched = False
        segments = path.split("/")
        parents = tuple(
            "/".join(segments[:index]) for index in range(1, len(segments))
        )
        for pattern in self.patterns:
            if pattern.exclusion != matched:
                continue
            pattern_matched = pattern.matches(path) or any(
                pattern.matches(parent) for parent in parents
            )
            if pattern_matched:
                matched = not pattern.exclusion
        return matched

    def match_with_parent_results(
        self, path: str, parent_results: tuple[bool, ...]
    ) -> tuple[bool, tuple[bool, ...]]:
        """Match a path while reusing per-pattern results from its parent."""

        if parent_results and len(parent_results) != len(self.patterns):
            raise DockerContextError("invalid .dockerignore parent match results")

        matched = False
        results = [False] * len(self.patterns)
        for index, pattern in enumerate(self.patterns):
            pattern_matched = (
                parent_results[index] if parent_results else False
            )
            if not pattern_matched:
                if pattern.exclusion != matched:
                    continue
                pattern_matched = pattern.matches(path)
            results[index] = pattern_matched
            if pattern_matched:
                matched = not pattern.exclusion
        return matched, tuple(results)


def _visible_entries_with_ancestors(
    raw_entries: tuple[DockerContextEntry, ...],
    ignore_matcher: _DockerIgnoreMatcher,
) -> tuple[DockerContextEntry, ...]:
    """Keep visible entries plus directory ancestors needed to reach them."""

    raw_by_path = {entry.path: entry for entry in raw_entries}
    active_directories: list[tuple[str, tuple[bool, ...]]] = []
    visible_paths: set[str] = set()
    tree_entries = sorted(
        raw_entries, key=lambda entry: tuple(entry.path.split("/"))
    )
    for entry in tree_entries:
        while active_directories and not entry.path.startswith(
            f"{active_directories[-1][0]}/"
        ):
            active_directories.pop()

        parent = posixpath.dirname(entry.path)
        if parent:
            if not active_directories or active_directories[-1][0] != parent:
                raise DockerContextError(
                    f"context walk omitted directory entry: {parent!r}"
                )
            parent_results = active_directories[-1][1]
        else:
            parent_results = ()

        ignored, match_results = ignore_matcher.match_with_parent_results(
            entry.path, parent_results
        )
        if entry.kind == "directory":
            active_directories.append((entry.path, match_results))
        if not ignored:
            visible_paths.add(entry.path)
    for path in tuple(visible_paths):
        parent = posixpath.dirname(path)
        while parent not in ("", "."):
            ancestor = raw_by_path[parent]
            if ancestor.kind != "directory":
                raise DockerContextError(
                    f"context walk yielded a file-as-ancestor path: {parent!r}"
                )
            visible_paths.add(parent)
            parent = posixpath.dirname(parent)
    return tuple(entry for entry in raw_entries if entry.path in visible_paths)


def _read_ignore_matcher(
    context: DockerContext, raw_files: tuple[str, ...]
) -> _DockerIgnoreMatcher:
    """Read and compile the active Dockerfile or root ignore file."""

    try:
        selected = context.dockerfile_ignore()
    except DockerContextError:
        raise
    except Exception as error:
        raise DockerContextError(
            "failed to obtain Dockerfile-specific ignore file"
        ) from error

    if selected is None:
        if ".dockerignore" not in raw_files:
            return _DockerIgnoreMatcher(())
        name = ".dockerignore"
        try:
            with context.open(name) as stream:
                content = stream.read()
        except DockerContextError:
            raise
        except Exception as error:
            raise DockerContextError(f"failed to read {name}") from error
    else:
        if (
            type(selected) is not tuple
            or len(selected) != 2
            or type(selected[0]) is not str
            or not selected[0]
            or type(selected[1]) is not bytes
        ):
            raise DockerContextError(
                "dockerfile_ignore() must return None or a "
                "(non-empty str, bytes) tuple"
            )
        name, content = selected

    try:
        lines = _prepare_ignore_lines(content.decode("utf-8-sig"))
        return _DockerIgnoreMatcher.from_lines(lines)
    except DockerContextError as error:
        raise DockerContextError(
            f"invalid active ignore file {name!r}: {error}"
        ) from error
    except Exception as error:
        raise DockerContextError(
            f"failed to read active ignore file {name!r}"
        ) from error


_MOBY_TRIM_SPACE = (
    " \t\n\v\f\r\u0085\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005"
    "\u2006\u2007\u2008\u2009\u200a\u2028"
    "\u2029\u202f\u205f\u3000"
)


def _trim_moby_space(value: str) -> str:
    """Trim exactly the Unicode White_Space set used by Go."""

    return value.strip(_MOBY_TRIM_SPACE)


def _prepare_ignore_lines(content: str) -> list[str]:
    """Apply Moby ignorefile preprocessing to .dockerignore content."""

    lines: list[str] = []
    for line_index, raw_line in enumerate(content.split("\n")):
        if line_index == 0:
            raw_line = raw_line.removeprefix("\ufeff")
        if raw_line.endswith("\r"):
            raw_line = raw_line[:-1]
        if raw_line.startswith("#"):
            continue

        pattern = _trim_moby_space(raw_line)
        if not pattern:
            continue

        invert = pattern.startswith("!")
        if invert:
            pattern = _trim_moby_space(pattern[1:])
        if pattern:
            pattern = _clean_posix_pattern(pattern)
            if len(pattern) > 1 and pattern.startswith("/"):
                pattern = pattern[1:]
        lines.append(("!" if invert else "") + pattern)
    return lines


def _clean_posix_pattern(pattern: str) -> str:
    """Return the Unix filepath.Clean equivalent used by Moby."""

    cleaned = posixpath.normpath(pattern)
    if cleaned.startswith("//"):
        cleaned = "/" + cleaned.lstrip("/")
    return cleaned


def _compile_ignore_pattern(line: str) -> _DockerIgnorePattern:
    """Compile one preprocessed pattern with Moby patternmatcher semantics."""

    cleaned = _clean_posix_pattern(_trim_moby_space(line))
    exclusion = cleaned.startswith("!")
    value = cleaned[1:] if exclusion else cleaned
    if not value:
        raise DockerContextError("invalid .dockerignore pattern: '!'")
    if "\0" in value:
        raise DockerContextError("invalid .dockerignore pattern containing NUL")

    tokens: list[_DockerIgnoreToken] = []
    match_type: Literal["exact", "prefix", "suffix", "tokens"] = "exact"
    index = 0
    token_index = 0
    while index < len(value):
        character = value[index]
        if character == "*":
            if index + 1 < len(value) and value[index + 1] == "*":
                index += 2
                if index < len(value) and value[index] == "/":
                    index += 1
                if index == len(value):
                    if match_type == "exact":
                        match_type = "prefix"
                    else:
                        tokens.append(_DockerIgnoreToken("globstar"))
                        match_type = "tokens"
                else:
                    tokens.append(_DockerIgnoreToken("globstar_directory"))
                    match_type = "tokens"
                if token_index == 0:
                    match_type = "suffix"
            else:
                tokens.append(_DockerIgnoreToken("star"))
                match_type = "tokens"
                index += 1
        elif character == "?":
            tokens.append(_DockerIgnoreToken("question"))
            match_type = "tokens"
            index += 1
        elif character == "[":
            character_class, index = _compile_ignore_character_class(
                value, index, line
            )
            tokens.append(_DockerIgnoreToken("class", character_class))
            match_type = "tokens"
        elif character == "\\":
            index += 1
            if index >= len(value):
                raise DockerContextError(
                    f"invalid .dockerignore pattern: {line!r}"
                )
            # Moby passes alphanumeric escapes through to RE2. Reject
            # them rather than silently downgrading their matching semantics.
            if (
                value[index] == "/"
                or value[index].isalnum()
                or value[index] == "_"
            ):
                raise DockerContextError(
                    f"unsupported .dockerignore escape: {line!r}"
                )
            tokens.append(_DockerIgnoreToken("literal", value[index]))
            match_type = "tokens"
            index += 1
        else:
            kind: Literal["literal", "anchor"] = (
                "anchor" if character == "^" else "literal"
            )
            tokens.append(_DockerIgnoreToken(kind, character))
            index += 1
        token_index += 1

    return _DockerIgnorePattern(value, exclusion, match_type, tuple(tokens))


def _compile_ignore_character_class(
    pattern: str, index: int, original: str
) -> tuple[_CharacterClass, int]:
    """Compile one strict Go filepath-compatible ignore character class."""

    index += 1
    negated = index < len(pattern) and pattern[index] == "^"
    if negated:
        index += 1

    ranges: list[tuple[str, str]] = []
    while True:
        if index >= len(pattern) or pattern[index] == "]":
            if index < len(pattern) and ranges:
                index += 1
                break
            raise DockerContextError(
                f"invalid .dockerignore pattern: {original!r}"
            )

        low, index, low_escaped = _read_ignore_class_character(
            pattern, index, original
        )
        if low == "-" and not low_escaped:
            raise DockerContextError(
                f"invalid .dockerignore pattern: {original!r}"
            )
        high = low
        if index < len(pattern) and pattern[index] == "-":
            index += 1
            if index >= len(pattern) or pattern[index] == "]":
                raise DockerContextError(
                    f"invalid .dockerignore pattern: {original!r}"
                )
            high, index, _ = _read_ignore_class_character(
                pattern, index, original
            )
            if ord(high) < ord(low):
                raise DockerContextError(
                    f"invalid .dockerignore pattern: {original!r}"
                )
        ranges.append((low, high))

    return _CharacterClass(negated, tuple(ranges)), index


def _read_ignore_class_character(
    pattern: str, index: int, original: str
) -> tuple[str, int, bool]:
    """Read one literal or escaped character inside an ignore class."""

    escaped = pattern[index] == "\\"
    if escaped:
        index += 1
        if index >= len(pattern):
            raise DockerContextError(
                f"invalid .dockerignore pattern: {original!r}"
            )
    character = pattern[index]
    if (
        character == "["
        or (escaped and (character.isalnum() or character in "_/"))
    ):
        raise DockerContextError(
            f"unsupported .dockerignore character class: {original!r}"
        )
    return character, index + 1, escaped


def _ignore_tokens_match(
    tokens: tuple[_DockerIgnoreToken, ...], path: str
) -> bool:
    """Match compiled tokens in O(pattern length * path length) time."""

    previous = [False] * (len(path) + 1)
    previous[0] = True
    for token in tokens:
        current = [False] * (len(path) + 1)
        if token.kind == "star":
            current[0] = previous[0]
            for path_index in range(1, len(path) + 1):
                current[path_index] = previous[path_index] or (
                    path[path_index - 1] != "/" and current[path_index - 1]
                )
        elif token.kind == "globstar":
            current[0] = previous[0]
            for path_index in range(1, len(path) + 1):
                current[path_index] = (
                    previous[path_index] or current[path_index - 1]
                )
        elif token.kind == "globstar_directory":
            previous_prefix = False
            for path_index in range(len(path) + 1):
                current[path_index] = previous[path_index]
                if path_index > 0:
                    previous_prefix = (
                        previous_prefix or previous[path_index - 1]
                    )
                    if path[path_index - 1] == "/" and previous_prefix:
                        current[path_index] = True
        elif token.kind == "anchor":
            current[0] = previous[0]
        elif token.kind in ("question", "literal", "class"):
            for path_index in range(1, len(path) + 1):
                character = path[path_index - 1]
                token_matches = False
                if token.kind == "question":
                    token_matches = character != "/"
                elif token.kind == "literal":
                    token_matches = character == token.value
                elif isinstance(token.value, _CharacterClass):
                    token_matches = token.value.matches(character)
                current[path_index] = (
                    previous[path_index - 1] and token_matches
                )
        else:
            raise DockerContextError("invalid compiled .dockerignore token")
        previous = current
    return previous[len(path)]


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


def _compile_source_pattern(
    pattern: str,
) -> tuple[tuple[str | _CharacterClass, ...], ...]:
    """Compile the strict no-escape subset of Go filepath-style source globs.

    Source normalization rejects backslashes, so escaped pattern syntax is not
    supported.
    """

    return tuple(
        _compile_glob_segment(segment, pattern) for segment in pattern.split("/")
    )


def _compile_glob_segment(
    segment: str, source: str
) -> tuple[str | _CharacterClass, ...]:
    """Compile one source pattern segment before context entries are inspected."""

    tokens: list[str | _CharacterClass] = []
    index = 0
    while index < len(segment):
        character = segment[index]
        if character == "[":
            character_class, index = _parse_character_class(segment, index, source)
            tokens.append(character_class)
            continue
        tokens.append(character)
        index += 1
    return tuple(tokens)


def _parse_character_class(
    segment: str, index: int, source: str
) -> tuple[_CharacterClass, int]:
    """Parse one non-empty Go filepath-style character class without escapes."""

    index += 1
    negated = index < len(segment) and segment[index] == "^"
    if negated:
        index += 1

    ranges: list[tuple[str, str]] = []
    while True:
        if index >= len(segment):
            raise DockerContextError(f"malformed context source pattern: {source!r}")
        if segment[index] == "]":
            if not ranges:
                raise DockerContextError(
                    f"malformed context source pattern: {source!r}"
                )
            return _CharacterClass(negated, tuple(ranges)), index + 1

        low = segment[index]
        if low == "-":
            raise DockerContextError(f"malformed context source pattern: {source!r}")
        index += 1
        high = low
        if index < len(segment) and segment[index] == "-":
            index += 1
            if index >= len(segment) or segment[index] in "-]":
                raise DockerContextError(
                    f"malformed context source pattern: {source!r}"
                )
            high = segment[index]
            index += 1
        ranges.append((low, high))


def _glob_matches(
    pattern_segments: tuple[tuple[str | _CharacterClass, ...], ...], path: str
) -> bool:
    """Match a compiled source pattern one POSIX path segment at a time."""

    path_segments = path.split("/")
    return len(pattern_segments) == len(path_segments) and all(
        _glob_segment_matches(pattern_segment, path_segment)
        for pattern_segment, path_segment in zip(
            pattern_segments, path_segments, strict=True
        )
    )


def _glob_segment_matches(
    pattern: tuple[str | _CharacterClass, ...], path: str
) -> bool:
    """Match one source segment; stars, questions, and classes never cross '/'."""

    pattern_index = 0
    path_index = 0
    star_index: int | None = None
    star_path_index = 0
    while path_index < len(path):
        if pattern_index < len(pattern):
            token = pattern[pattern_index]
            if token == "*":
                star_index = pattern_index
                star_path_index = path_index
                pattern_index += 1
                continue
            if token == "?":
                pattern_index += 1
                path_index += 1
                continue
            if _glob_token_matches(token, path[path_index]):
                pattern_index += 1
                path_index += 1
                continue
        if star_index is None:
            return False
        star_path_index += 1
        path_index = star_path_index
        pattern_index = star_index + 1

    return all(token == "*" for token in pattern[pattern_index:])


def _glob_token_matches(token: str | _CharacterClass, character: str) -> bool:
    """Return whether one literal or character class token matches a codepoint."""

    if isinstance(token, _CharacterClass):
        return token.matches(character)
    return token == character


def _validate_selection_entries(
    entries: tuple[_SelectedContextEntry, ...] | list[_SelectedContextEntry],
) -> tuple[_SelectedContextEntry, ...]:
    """Sort selection entries and reject source or target collisions."""

    ordered = tuple(sorted(entries, key=lambda item: item.source_path))
    source_paths = [item.source_path for item in ordered]
    target_paths = [
        item.relative_target
        for item in ordered
        if not (item.kind == "directory" and item.relative_target == "")
    ]
    if len(source_paths) != len(set(source_paths)):
        raise DockerContextError("context selection has duplicate source paths")
    if len(target_paths) != len(set(target_paths)):
        raise DockerContextError("context selection has colliding target paths")
    return ordered


@contextmanager
def _open_absolute_regular_file(path: str) -> Iterator[BinaryIO]:
    """Open an absolute regular file without following symbolic links."""

    if not _SECURE_OPEN_SUPPORTED:
        raise DockerContextError(
            "secure Dockerfile-specific ignore file opening is not supported "
            "on this platform"
        )
    absolute_path = os.path.abspath(path)
    segments = tuple(segment for segment in absolute_path.split(os.sep) if segment)
    if not segments:
        raise DockerContextError("Dockerfile-specific ignore path must name a file")

    common_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags = common_flags | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = common_flags | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    directory_fds: list[int] = []
    final_fd: int | None = None
    handle: BinaryIO | None = None
    try:
        root_fd = os.open(os.sep, directory_flags)
        directory_fds.append(root_fd)
        parent_fd = root_fd
        for segment in segments[:-1]:
            parent_fd = os.open(segment, directory_flags, dir_fd=parent_fd)
            directory_fds.append(parent_fd)
        final_fd = os.open(segments[-1], file_flags, dir_fd=parent_fd)
        if not stat.S_ISREG(os.fstat(final_fd).st_mode):
            raise DockerContextError(
                "Dockerfile-specific ignore path is not a regular file: "
                f"{absolute_path!r}"
            )
        handle = os.fdopen(final_fd, "rb")
        final_fd = None
    except DockerContextError:
        raise
    except (OSError, TypeError, ValueError) as error:
        raise DockerContextError(
            "cannot securely open Dockerfile-specific ignore file: "
            f"{absolute_path!r}"
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

    def dockerfile_ignore(self) -> tuple[str, bytes] | None:
        """Return the adjacent Dockerfile-specific ignore file, if present."""

        if not self._dockerfile_path:
            return None
        companion_path = f"{self._dockerfile_path}.dockerignore"
        try:
            info = os.lstat(companion_path)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise DockerContextError(
                "cannot inspect Dockerfile-specific ignore file: "
                f"{companion_path!r}"
            ) from error
        if not stat.S_ISREG(info.st_mode):
            raise DockerContextError(
                "Dockerfile-specific ignore path is not a regular file: "
                f"{companion_path!r}"
            )

        try:
            relative_path = os.path.relpath(companion_path, self._context_dir)
        except ValueError:
            relative_path = os.pardir
        inside_context = relative_path != os.pardir and not relative_path.startswith(
            f"{os.pardir}{os.sep}"
        )
        name = _to_posix(relative_path) if inside_context else companion_path
        try:
            if inside_context:
                with self.open(name) as stream:
                    content = stream.read()
            else:
                with _open_absolute_regular_file(companion_path) as stream:
                    content = stream.read()
        except DockerContextError:
            raise
        except Exception as error:
            raise DockerContextError(
                "failed to read Dockerfile-specific ignore file: "
                f"{name!r}"
            ) from error
        return name, content

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
        def traversal_error(error: OSError, path: str) -> DockerContextError:
            candidate = error.filename or path
            try:
                detail = _to_posix(os.path.relpath(candidate, self._context_dir))
            except (TypeError, ValueError):
                detail = str(candidate)
            return DockerContextError(
                f"cannot traverse Docker context directory: {detail!r}"
            )

        def onerror(error: OSError) -> None:
            raise traversal_error(error, self._context_dir) from error

        entries: list[DockerContextEntry] = []
        for root, dirs, files in os.walk(self._context_dir, onerror=onerror):
            dirs.sort()
            for name in dirs:
                full = os.path.join(root, name)
                try:
                    info = os.lstat(full)
                except OSError as error:
                    raise traversal_error(error, full) from error
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
                try:
                    info = os.lstat(full)
                except OSError as error:
                    raise traversal_error(error, full) from error
                if stat.S_ISLNK(info.st_mode):
                    raise DockerContextError(
                        f"context contains a symbolic link: {name!r}"
                    )
                if not stat.S_ISREG(info.st_mode):
                    raise DockerContextError(
                        f"context path is not a regular file: {name!r}"
                    )
                rel = _to_posix(os.path.relpath(full, self._context_dir))
                entries.append(
                    DockerContextEntry(rel, "file", stat.S_IMODE(info.st_mode))
                )
        yield from sorted(entries, key=lambda entry: entry.path)

    @property
    def context_dir(self) -> str:
        """Absolute path to the local context root."""

        return self._context_dir
