"""Unit tests for Docker build context manifests and source selection."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO
from unittest.mock import patch

from akernel_sdk._dockercontext import (
    DockerContext,
    DockerContextError,
    LocalDockerContext,
    _ContextManifest,
)

NL = bytes([10])
CRLF = bytes([13, 10])


class MemoryDockerContext(DockerContext):
    """A context fixture that records manifest file opens."""

    def __init__(self, files: dict[str, bytes], paths: list[object] | None = None):
        self.files = files
        self.paths = list(files) if paths is None else paths
        self.open_paths: list[str] = []

    def dockerfile_text(self) -> str:
        return "FROM scratch"

    @contextmanager
    def open(self, path: str) -> Iterator[BinaryIO]:
        self.open_paths.append(path)
        yield io.BytesIO(self.files[path])

    def walk(self) -> Iterator[str]:
        yield from self.paths  # type: ignore[misc]


def paths(selection) -> list[tuple[str, str]]:
    return [(item.source_path, item.relative_target) for item in selection.files]


class TestContextManifest(unittest.TestCase):
    def test_deterministic_literal_file_directory_and_dot(self) -> None:
        context = MemoryDockerContext(
            {"src/lib/b.py": b"", "root.txt": b"", "src/a.py": b""},
            ["src/lib/b.py", "root.txt", "src/a.py"],
        )
        manifest = _ContextManifest.from_context(context)
        self.assertEqual(context.open_paths, [])
        self.assertEqual(paths(manifest.select("src/a.py")), [("src/a.py", "a.py")])
        self.assertEqual(
            paths(manifest.select("./src//")),
            [("src/a.py", "a.py"), ("src/lib/b.py", "lib/b.py")],
        )
        self.assertEqual(
            paths(manifest.select(".")),
            [
                ("root.txt", "root.txt"),
                ("src/a.py", "src/a.py"),
                ("src/lib/b.py", "src/lib/b.py"),
            ],
        )
        self.assertEqual(context.open_paths, [])

    def test_dockerignore_semantics_and_reserved_files(self) -> None:
        ignore = (
            bytes([0xEF, 0xBB, 0xBF])
            + b"# comment"
            + CRLF
            + CRLF
            + b"."
            + CRLF
            + b".env"
            + CRLF
            + b".git/"
            + CRLF
            + b"*.txt"
            + CRLF
            + b"!keep.txt"
            + CRLF
            + b"**/generated.py"
            + CRLF
            + b"/anchored/"
            + CRLF
            + b"tail/"
            + CRLF
        )
        context = MemoryDockerContext(
            {
                ".dockerignore": ignore,
                "Dockerfile": b"",
                ".env": b"",
                ".git/config": b"",
                "drop.txt": b"",
                "keep.txt": b"",
                "src/generated.py": b"",
                "anchored/a": b"",
                "tail/a": b"",
                "src/ok.py": b"",
            }
        )
        manifest = _ContextManifest.from_context(context)
        self.assertEqual(context.open_paths, [".dockerignore"])
        self.assertEqual(
            paths(manifest.select(".")),
            [("keep.txt", "keep.txt"), ("src/ok.py", "src/ok.py")],
        )
        for source in (".dockerignore", "Dockerfile", ".env", ".git", "drop.txt"):
            with self.subTest(source=source):
                with self.assertRaisesRegex(DockerContextError, "ignored"):
                    manifest.select(source)
        self.assertEqual(context.open_paths, [".dockerignore"])

    def test_escaped_ignore_patterns_and_invalid_utf8(self) -> None:
        slash = chr(92).encode()
        escaped = MemoryDockerContext(
            {
                ".dockerignore": slash + b"#secret" + NL + slash + b"!secret" + NL,
                "#secret": b"",
                "!secret": b"",
                "visible": b"",
            }
        )
        self.assertEqual(
            paths(_ContextManifest.from_context(escaped).select(".")),
            [("visible", "visible")],
        )
        invalid = MemoryDockerContext({".dockerignore": bytes([0xFF]), "ok": b""})
        with self.assertRaisesRegex(DockerContextError, "dockerignore"):
            _ContextManifest.from_context(invalid)

    def test_wildcards_directories_and_collisions(self) -> None:
        manifest = _ContextManifest.from_context(
            MemoryDockerContext(
                {
                    "top.py": b"",
                    "src/a.py": b"",
                    "src/lib/b.py": b"",
                    "modules/one/x.py": b"",
                    "modules/two/y.txt": b"",
                    "one/x.py": b"",
                    "two/x.py": b"",
                }
            )
        )
        self.assertEqual(paths(manifest.select("*.py")), [("top.py", "top.py")])
        self.assertEqual(
            paths(manifest.select("src/**/*.py")),
            [("src/a.py", "a.py"), ("src/lib/b.py", "b.py")],
        )
        self.assertEqual(
            paths(manifest.select("modules/*")),
            [("modules/one/x.py", "one/x.py"), ("modules/two/y.txt", "two/y.txt")],
        )
        self.assertEqual(
            paths(manifest.select("modules/**")),
            [
                ("modules/one/x.py", "modules/one/x.py"),
                ("modules/two/y.txt", "modules/two/y.txt"),
            ],
        )
        with self.assertRaisesRegex(DockerContextError, "colliding"):
            manifest.select("*/*.py")

    def test_wildcard_no_match_and_only_ignored(self) -> None:
        manifest = _ContextManifest.from_context(
            MemoryDockerContext(
                {".dockerignore": b"*.py" + NL, "hidden.py": b"", "visible.txt": b""}
            )
        )
        with self.assertRaisesRegex(DockerContextError, "ignored"):
            manifest.select("*.py")
        with self.assertRaisesRegex(DockerContextError, "no match"):
            manifest.select("*.go")

    def test_dot_distinguishes_empty_and_fully_filtered_contexts(self) -> None:
        cases = (
            ({}, "no match"),
            ({"Dockerfile": b""}, "ignored"),
            ({".dockerignore": b"*.txt" + NL, "hidden.txt": b""}, "ignored"),
        )
        for files, message in cases:
            with self.subTest(files=files):
                manifest = _ContextManifest.from_context(MemoryDockerContext(files))
                with self.assertRaisesRegex(DockerContextError, message):
                    manifest.select(".")

    def test_malicious_walk_paths_fail_closed(self) -> None:
        invalid: list[object] = [
            1,
            "",
            "/absolute",
            "directory/",
            "../parent",
            "a/../b",
            "./file",
            "a//b",
            "a" + chr(92) + "b",
            "a" + chr(0) + "b",
        ]
        for value in invalid:
            with self.subTest(value=repr(value)):
                with self.assertRaises(DockerContextError):
                    _ContextManifest.from_context(MemoryDockerContext({}, [value]))
        with self.assertRaises(DockerContextError):
            _ContextManifest.from_context(MemoryDockerContext({}, ["same", "same"]))

    def test_malicious_sources_fail_closed(self) -> None:
        manifest = _ContextManifest.from_context(MemoryDockerContext({"safe": b""}))
        for source in (
            "",
            "/absolute",
            "../parent",
            "a/../b",
            "a" + chr(92) + "b",
            "a" + chr(0),
        ):
            with self.subTest(source=repr(source)):
                with self.assertRaises(DockerContextError):
                    manifest.select(source)

    def test_local_and_memory_contexts_match(self) -> None:
        files = {
            ".dockerignore": b"*.tmp" + NL + b"!keep.tmp" + NL,
            "Dockerfile": b"",
            "src/a.py": b"",
            "src/cache.tmp": b"",
            "keep.tmp": b"",
        }
        expected = _ContextManifest.from_context(MemoryDockerContext(files)).select(".")
        with tempfile.TemporaryDirectory() as directory:
            for name, content in files.items():
                path = Path(directory, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            actual = _ContextManifest.from_context(
                LocalDockerContext("FROM scratch", context_dir=directory)
            ).select(".")
        self.assertEqual(expected, actual)

    def test_local_walk_determinism_and_symlink_protection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "z").write_bytes(b"")
            Path(directory, "a").write_bytes(b"")
            local = LocalDockerContext("FROM scratch", context_dir=directory)
            self.assertEqual(list(local.walk()), ["a", "z"])
            outside = Path(directory).parent / "dockercontext-outside"
            outside.write_bytes(b"outside")
            try:
                os.symlink(outside, Path(directory, "link"))
                with self.assertRaisesRegex(DockerContextError, "symbolic link"):
                    list(local.walk())
                with self.assertRaisesRegex(DockerContextError, "securely open"):
                    with local.open("link"):
                        pass
            finally:
                outside.unlink(missing_ok=True)

    def test_local_open_rejects_symlink_replacement_race(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context_dir = root / "context"
            context_dir.mkdir()
            selected = context_dir / "selected"
            selected.write_bytes(b"safe")
            (root / "host-secret").write_bytes(b"host-secret")
            local = LocalDockerContext("FROM scratch", context_dir=context_dir)
            real_open = os.open
            replaced = False

            def racing_open(
                path: str | bytes,
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal replaced
                if path == "selected" and dir_fd is not None and not replaced:
                    replaced = True
                    selected.unlink()
                    selected.symlink_to("../host-secret")
                if dir_fd is None:
                    return real_open(path, flags, mode)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with (
                patch(
                    "akernel_sdk._dockercontext.os.open",
                    side_effect=racing_open,
                ),
                self.assertRaisesRegex(DockerContextError, "securely open"),
            ):
                with local.open("selected") as stream:
                    self.fail(f"escaped context: {stream.read()!r}")
            self.assertTrue(replaced)

    def test_local_open_accepts_nested_regular_files_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            nested = Path(directory, "nested")
            nested.mkdir()
            Path(nested, "file").write_bytes(b"ok")
            local = LocalDockerContext("FROM scratch", context_dir=directory)
            with local.open("nested/file") as stream:
                self.assertEqual(stream.read(), b"ok")
            with self.assertRaisesRegex(DockerContextError, "regular file"):
                with local.open("nested"):
                    pass
            for unsafe in ("/absolute", "../parent", "a\\b", "nul\0path"):
                with self.subTest(path=unsafe):
                    with self.assertRaises(DockerContextError):
                        with local.open(unsafe):
                            pass
