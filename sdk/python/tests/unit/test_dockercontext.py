"""Unit tests for Docker build context manifests and source selection."""

from __future__ import annotations

import errno
import io
import os
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO
from unittest.mock import patch

import akernel_sdk._dockercontext as dockercontext_module
from akernel_sdk._dockercontext import (
    DockerContext,
    DockerContextEntry,
    DockerContextError,
    LocalDockerContext,
    _ContextManifest,
)

NL = bytes([10])
CRLF = bytes([13, 10])


class MemoryDockerContext(DockerContext):
    """A context fixture that records manifest file opens."""

    def __init__(
        self,
        files: dict[str, bytes],
        paths: list[object] | None = None,
        dockerfile_ignore: tuple[str, bytes] | None = None,
    ):
        self.files = files
        self.paths = _entries(files) if paths is None else paths
        self._dockerfile_ignore = dockerfile_ignore
        self.open_paths: list[str] = []

    def dockerfile_text(self) -> str:
        return "FROM scratch"

    def dockerfile_ignore(self) -> tuple[str, bytes] | None:
        return self._dockerfile_ignore

    @contextmanager
    def open(self, path: str) -> Iterator[BinaryIO]:
        self.open_paths.append(path)
        yield io.BytesIO(self.files[path])

    def walk(self) -> Iterator[DockerContextEntry]:
        yield from self.paths  # type: ignore[misc]


def _entries(files: dict[str, bytes]) -> list[DockerContextEntry]:
    directories = {
        "/".join(path.split("/")[:index])
        for path in files
        for index in range(1, len(path.split("/")))
    }
    return [
        *(DockerContextEntry(path, "directory", 0o755) for path in sorted(directories)),
        *(DockerContextEntry(path, "file", 0o644) for path in sorted(files)),
    ]


def paths(selection) -> list[tuple[str, str]]:
    return [
        (item.source_path, item.relative_target)
        for item in selection.entries
        if item.kind == "file"
    ]


class TestContextManifest(unittest.TestCase):
    def test_deterministic_literal_file_directory_and_dot(self) -> None:
        context = MemoryDockerContext(
            {"src/lib/b.py": b"", "root.txt": b"", "src/a.py": b""},
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

    def test_dockerfile_specific_ignore_precedence_and_hook_failures(self) -> None:
        files = {
            ".dockerignore": b"root-only\n",
            "docker/custom.Dockerfile": b"FROM scratch\n",
            "docker/custom.Dockerfile.dockerignore": b"companion-only\n",
            "companion-only": b"",
            "root-only": b"",
        }
        cases = (
            (("docker/custom.Dockerfile.dockerignore", b"companion-only\n"),
             [".dockerignore", "docker/custom.Dockerfile",
              "docker/custom.Dockerfile.dockerignore", "root-only"], []),
            (("docker/custom.Dockerfile.dockerignore", b""), sorted(files), []),
            (None, [".dockerignore", "companion-only", "docker/custom.Dockerfile",
                    "docker/custom.Dockerfile.dockerignore"], [".dockerignore"]),
        )
        for selected, expected, opened in cases:
            with self.subTest(selected=selected):
                context = MemoryDockerContext(files, dockerfile_ignore=selected)
                manifest = _ContextManifest.from_context(context)
                self.assertEqual(
                    [path for path, _ in paths(manifest.select("."))],
                    expected,
                )
                self.assertEqual(context.open_paths, opened)

        bad_pattern = MemoryDockerContext(
            {"safe": b""}, dockerfile_ignore=("custom.Dockerfile.dockerignore", b"[\n")
        )
        with self.assertRaisesRegex(
            DockerContextError, "custom.Dockerfile.dockerignore"
        ):
            _ContextManifest.from_context(bad_pattern)

        class RaisingContext(MemoryDockerContext):
            def __init__(self, error: Exception) -> None:
                super().__init__({"safe": b""})
                self.error = error

            def dockerfile_ignore(self) -> tuple[str, bytes] | None:
                raise self.error

        with self.assertRaisesRegex(DockerContextError, "failed to obtain"):
            _ContextManifest.from_context(RaisingContext(OSError("unreadable")))
        with self.assertRaisesRegex(DockerContextError, "sentinel"):
            _ContextManifest.from_context(
                RaisingContext(DockerContextError("sentinel"))
            )

        for value in ([], ("name",), ("name", b"", b"x"), ("", b""),
                      (1, b""), ("name", "text"), ["name", b""]):
            with self.subTest(value=repr(value)):
                context = MemoryDockerContext({"safe": b""})
                context._dockerfile_ignore = value  # type: ignore[assignment]
                with self.assertRaisesRegex(DockerContextError, "dockerfile_ignore"):
                    _ContextManifest.from_context(context)

    def test_default_dockerfile_ignore_method_remains_root_compatible(self) -> None:
        class DefaultIgnoreContext(DockerContext):
            def dockerfile_text(self) -> str:
                return "FROM scratch"

            @contextmanager
            def open(self, path: str) -> Iterator[BinaryIO]:
                yield io.BytesIO({".dockerignore": b"hidden\n", "hidden": b""}[path])

            def walk(self) -> Iterator[DockerContextEntry]:
                yield DockerContextEntry(".dockerignore", "file", 0o644)
                yield DockerContextEntry("hidden", "file", 0o644)

        manifest = _ContextManifest.from_context(DefaultIgnoreContext())
        with self.assertRaisesRegex(DockerContextError, "ignored"):
            manifest.select("hidden")

    def test_local_companion_visibility_and_explicit_control_exclusion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dockerfile = root / "docker" / "custom.Dockerfile"
            dockerfile.parent.mkdir()
            dockerfile.write_text("FROM scratch\n", encoding="utf-8")
            (root / ".dockerignore").write_text("root-only\n", encoding="utf-8")
            companion = dockerfile.with_name("custom.Dockerfile.dockerignore")
            companion.write_text("companion-only\n", encoding="utf-8")
            for name in ("root-only", "companion-only", "visible"):
                (root / name).write_text(name, encoding="utf-8")

            context = LocalDockerContext(dockerfile, context_dir=root)
            manifest = _ContextManifest.from_context(context)
            self.assertEqual(
                [path for path, _ in paths(manifest.select("."))],
                [".dockerignore", "docker/custom.Dockerfile",
                 "docker/custom.Dockerfile.dockerignore", "root-only", "visible"],
            )
            for name in (".dockerignore", "docker/custom.Dockerfile",
                         "docker/custom.Dockerfile.dockerignore"):
                self.assertEqual(
                    paths(manifest.select(name)),
                    [(name, Path(name).name)],
                )

            companion.write_text(
                ".dockerignore\ndocker/custom.Dockerfile\n"
                "docker/custom.Dockerfile.dockerignore\n", encoding="utf-8"
            )
            filtered = _ContextManifest.from_context(
                LocalDockerContext(dockerfile, context_dir=root)
            )
            for name in (".dockerignore", "docker/custom.Dockerfile",
                         "docker/custom.Dockerfile.dockerignore"):
                with self.assertRaisesRegex(DockerContextError, "ignored"):
                    filtered.select(name)

    def test_inline_and_external_dockerfiles_are_not_synthesized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
            (root / "Dockerfile").chmod(0o644)
            self.assertEqual(
                list(LocalDockerContext("FROM scratch\n", context_dir=root).walk()),
                [DockerContextEntry("Dockerfile", "file", 0o644)],
            )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context_dir = root / "context"
            context_dir.mkdir()
            dockerfile = root / "outside.Dockerfile"
            dockerfile.write_text("FROM scratch\n", encoding="utf-8")
            dockerfile.with_name("outside.Dockerfile.dockerignore").write_text(
                "hidden\n", encoding="utf-8"
            )
            (context_dir / ".dockerignore").write_text("visible\n", encoding="utf-8")
            (context_dir / "hidden").write_text("", encoding="utf-8")
            (context_dir / "visible").write_text("", encoding="utf-8")
            manifest = _ContextManifest.from_context(
                LocalDockerContext(dockerfile, context_dir=context_dir)
            )
            self.assertEqual(
                [entry.path for entry in manifest._raw_entries],
                [".dockerignore", "hidden", "visible"],
            )
            self.assertEqual(
                [path for path, _ in paths(manifest.select("."))],
                [".dockerignore", "visible"],
            )

    def test_context_companion_non_regular_files_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dockerfile = root / "Dockerfile"
            dockerfile.write_text("FROM scratch\n", encoding="utf-8")
            companion = root / "Dockerfile.dockerignore"
            local = LocalDockerContext(dockerfile, context_dir=root)
            target = root / "target"
            target.write_text("", encoding="utf-8")
            companion.symlink_to(target)
            with self.assertRaisesRegex(DockerContextError, "regular file"):
                local.dockerfile_ignore()
            companion.unlink()
            companion.mkdir()
            with self.assertRaisesRegex(DockerContextError, "regular file"):
                local.dockerfile_ignore()
            companion.rmdir()
            if not hasattr(os, "mkfifo"):
                self.skipTest("mkfifo is unavailable on this platform")
            try:
                os.mkfifo(companion)
            except (NotImplementedError, OSError) as error:
                self.skipTest(f"mkfifo is unavailable: {error}")
            try:
                with self.assertRaisesRegex(DockerContextError, "regular file"):
                    local.dockerfile_ignore()
            finally:
                companion.unlink()

    def test_context_companion_lstat_symlink_race_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dockerfile = root / "Dockerfile"
            dockerfile.write_text("FROM scratch\n", encoding="utf-8")
            companion = root / "Dockerfile.dockerignore"
            companion.write_bytes(b"safe\n")
            target = root / "target"
            target.write_bytes(b"target\n")
            local = LocalDockerContext(dockerfile, context_dir=root)
            real_lstat = os.lstat
            replaced = False

            def racing_lstat(
                path: str | bytes,
                *,
                dir_fd: int | None = None,
            ) -> os.stat_result:
                nonlocal replaced
                info = real_lstat(path, dir_fd=dir_fd)
                if (
                    os.path.abspath(os.fsdecode(path)) == str(companion)
                    and not replaced
                ):
                    replaced = True
                    companion.unlink()
                    companion.symlink_to(target)
                return info

            with (
                patch(
                    "akernel_sdk._dockercontext.os.lstat",
                    side_effect=racing_lstat,
                ),
                self.assertRaises(DockerContextError),
            ):
                local.dockerfile_ignore()
            self.assertTrue(replaced)

    def test_dockerignore_semantics_and_control_file_visibility(self) -> None:
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
            [
                (".dockerignore", ".dockerignore"),
                ("Dockerfile", "Dockerfile"),
                ("keep.txt", "keep.txt"),
                ("src/ok.py", "src/ok.py"),
            ],
        )
        for source in (".env", ".git", "drop.txt"):
            with self.subTest(source=source):
                with self.assertRaisesRegex(DockerContextError, "ignored"):
                    manifest.select(source)
        self.assertEqual(context.open_paths, [".dockerignore"])

    def test_dockerignore_moby_preprocessing_and_embedded_double_star(self) -> None:
        context = MemoryDockerContext(
            {
                ".dockerignore": (
                    b"# column-one comment\n"
                    b"  #secret\n"
                    b"a**/*.txt\n"
                    b"foo/../cleaned-secret\n"
                    b"[^/]*.pem\n"
                    b"foo[/]bar\n"
                ),
                "#secret": b"hidden",
                "a/file.txt": b"hidden",
                "a/dir/dir/secret.txt": b"hidden",
                "a/keep.bin": b"visible",
                "cleaned-secret": b"hidden",
                "foo/bar": b"hidden",
                "nested/root.pem": b"visible",
                "root.pem": b"hidden",
                "safe": b"visible",
            }
        )
        manifest = _ContextManifest.from_context(context)
        self.assertEqual(
            paths(manifest.select(".")),
            [
                (".dockerignore", ".dockerignore"),
                ("a/keep.bin", "a/keep.bin"),
                ("nested/root.pem", "nested/root.pem"),
                ("safe", "safe"),
            ],
        )
        for source in (
            "#secret",
            "a/file.txt",
            "a/dir/dir/secret.txt",
            "cleaned-secret",
            "foo/bar",
            "root.pem",
        ):
            with self.subTest(source=source):
                with self.assertRaisesRegex(DockerContextError, "ignored"):
                    manifest.select(source)
        self.assertEqual(context.open_paths, [".dockerignore"])

    def test_reincluded_descendants_make_virtual_source_directories(self) -> None:
        context = MemoryDockerContext(
            {
                ".dockerignore": (
                    b"docs\n"
                    b"!docs/README.md\n"
                    b"!docs/nested/guide.md\n"
                ),
                "docs/README.md": b"visible",
                "docs/nested/guide.md": b"visible",
                "docs/private.txt": b"hidden",
            }
        )
        manifest = _ContextManifest.from_context(context)
        expected = [
            ("docs", "", "directory"),
            ("docs/README.md", "README.md", "file"),
            ("docs/nested", "nested", "directory"),
            ("docs/nested/guide.md", "nested/guide.md", "file"),
        ]
        literal = manifest.select("docs")
        self.assertEqual(
            [
                (entry.source_path, entry.relative_target, entry.kind)
                for entry in literal.entries
            ],
            expected,
        )
        wildcard = manifest.select("*")
        self.assertEqual(
            [
                (entry.source_path, entry.relative_target, entry.kind)
                for entry in wildcard.entries
            ],
            [(".dockerignore", ".dockerignore", "file"), *expected],
        )
        self.assertEqual(wildcard.top_level_source_count, 2)
        with self.assertRaisesRegex(DockerContextError, "ignored"):
            manifest.select("docs/private.txt")
        self.assertEqual(context.open_paths, [".dockerignore"])

    def test_malformed_dockerignore_patterns_fail_closed(self) -> None:
        for pattern in (b"!\n", b"[\n", b"trailing\\\n", b"[z-a]\n"):
            with self.subTest(pattern=pattern):
                context = MemoryDockerContext(
                    {".dockerignore": pattern, "safe": b"visible"}
                )
                with self.assertRaisesRegex(
                    DockerContextError, "invalid .dockerignore pattern"
                ):
                    _ContextManifest.from_context(context)
                self.assertEqual(context.open_paths, [".dockerignore"])


    def test_unsupported_dockerignore_regex_escapes_fail_closed(self) -> None:
        cases = (
            (br"\d*.pem" + NL, "unsupported .dockerignore escape"),
            (br"\s*" + NL, "unsupported .dockerignore escape"),
            (br"\x41*" + NL, "unsupported .dockerignore escape"),
            (br"\foo" + NL, "unsupported .dockerignore escape"),
            (br"[\d]*.pem" + NL, "unsupported .dockerignore character class"),
            (b"[[:digit:]]*.pem" + NL, "unsupported .dockerignore character class"),
        )
        for pattern, message in cases:
            with self.subTest(pattern=pattern):
                context = MemoryDockerContext(
                    {".dockerignore": pattern, "1secret.pem": b"hidden"}
                )
                with self.assertRaisesRegex(DockerContextError, message):
                    _ContextManifest.from_context(context)
                self.assertEqual(context.open_paths, [".dockerignore"])

    def test_parent_match_stack_handles_path_prefix_siblings(self) -> None:
        context = MemoryDockerContext(
            {
                ".dockerignore": b"never-match*\n",
                "a/file": b"nested",
                "a-b": b"sibling",
            }
        )
        manifest = _ContextManifest.from_context(context)
        self.assertEqual(
            paths(manifest.select(".")),
            [
                (".dockerignore", ".dockerignore"),
                ("a-b", "a-b"),
                ("a/file", "a/file"),
            ],
        )

    def test_manifest_reuses_parent_ignore_results(self) -> None:
        nested = "/".join(f"level-{index}" for index in range(40))
        path = f"{nested}/visible.txt"
        context = MemoryDockerContext(
            {".dockerignore": b"never-match*\n", path: b"visible"}
        )
        with patch.object(
            dockercontext_module,
            "_ignore_tokens_match",
            wraps=dockercontext_module._ignore_tokens_match,
        ) as token_match:
            manifest = _ContextManifest.from_context(context)
        self.assertEqual(paths(manifest.select(path)), [(path, "visible.txt")])
        self.assertLessEqual(token_match.call_count, len(context.paths))

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
            [
                (".dockerignore", ".dockerignore"),
                ("visible", "visible"),
            ],
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
            [("src/lib/b.py", "b.py")],
        )
        wildcard_directories = manifest.select("modules/**")
        self.assertEqual(
            [
                (item.source_path, item.relative_target, item.kind)
                for item in wildcard_directories.entries
            ],
            [
                ("modules/one", "", "directory"),
                ("modules/one/x.py", "x.py", "file"),
                ("modules/two", "", "directory"),
                ("modules/two/y.txt", "y.txt", "file"),
            ],
        )
        self.assertEqual(
            paths(wildcard_directories),
            [("modules/one/x.py", "x.py"), ("modules/two/y.txt", "y.txt")],
        )
        self.assertEqual(
            paths(manifest.select("modules/*")),
            paths(wildcard_directories),
        )
        with self.assertRaisesRegex(DockerContextError, "colliding"):
            manifest.select("*/*.py")

    def test_wildcard_character_classes_follow_go_filepath_semantics(self) -> None:
        manifest = _ContextManifest.from_context(
            MemoryDockerContext(
                {
                    "!.txt": b"",
                    "a.txt": b"",
                    "b.txt": b"",
                    "c.txt": b"",
                    "é.txt": b"",
                    "nested/x.txt": b"",
                    "nested/deep/y.txt": b"",
                }
            )
        )
        self.assertEqual(
            paths(manifest.select("[!a].txt")),
            [("!.txt", "!.txt"), ("a.txt", "a.txt")],
        )
        self.assertEqual(
            paths(manifest.select("[^a].txt")),
            [
                ("!.txt", "!.txt"),
                ("b.txt", "b.txt"),
                ("c.txt", "c.txt"),
                ("é.txt", "é.txt"),
            ],
        )
        self.assertEqual(
            paths(manifest.select("[a-c].txt")),
            [("a.txt", "a.txt"), ("b.txt", "b.txt"), ("c.txt", "c.txt")],
        )
        self.assertEqual(
            paths(manifest.select("?.txt")),
            [
                ("!.txt", "!.txt"),
                ("a.txt", "a.txt"),
                ("b.txt", "b.txt"),
                ("c.txt", "c.txt"),
                ("é.txt", "é.txt"),
            ],
        )
        self.assertEqual(
            paths(manifest.select("**.txt")), paths(manifest.select("*.txt"))
        )
        self.assertEqual(
            paths(manifest.select("*/?.txt")), [("nested/x.txt", "x.txt")]
        )

    def test_malformed_wildcard_patterns_fail_closed_before_file_reads(self) -> None:
        for files in ({}, {"safe.txt": b""}):
            for pattern in (
                "file[.txt",
                "[]",
                "[^]",
                "[a-]",
                "[-a]",
                "[a-b-c]",
            ):
                with self.subTest(files=files, pattern=pattern):
                    context = MemoryDockerContext(files)
                    manifest = _ContextManifest.from_context(context)
                    with self.assertRaisesRegex(
                        DockerContextError, "malformed context source pattern"
                    ):
                        manifest.select(pattern)
                    self.assertEqual(context.open_paths, [])

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
            ({".dockerignore": b"*" + NL, "hidden.txt": b""}, "ignored"),
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
                    _ContextManifest.from_context(
                        MemoryDockerContext(
                            {},
                            [
                                DockerContextEntry(  # type: ignore[arg-type]
                                    value, "file", 0o644
                                )
                            ],
                        )
                    )
        with self.assertRaises(DockerContextError):
            duplicate = DockerContextEntry("same", "file", 0o644)
            _ContextManifest.from_context(
                MemoryDockerContext({}, [duplicate, duplicate])
            )

    def test_walk_requires_explicit_directory_ancestors(self) -> None:
        missing_parent = [
            DockerContextEntry("nested/file", "file", 0o644),
        ]
        with self.assertRaisesRegex(DockerContextError, "omitted directory"):
            _ContextManifest.from_context(MemoryDockerContext({}, missing_parent))

        file_ancestor = [
            DockerContextEntry("nested", "file", 0o644),
            DockerContextEntry("nested/file", "file", 0o644),
        ]
        with self.assertRaisesRegex(DockerContextError, "file-as-ancestor"):
            _ContextManifest.from_context(MemoryDockerContext({}, file_ancestor))

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
                os.chmod(path, 0o644)
            os.chmod(Path(directory, "src"), 0o755)
            actual = _ContextManifest.from_context(
                LocalDockerContext("FROM scratch", context_dir=directory)
            ).select(".")
        self.assertEqual(expected, actual)

    def test_local_walk_determinism_and_symlink_protection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "z").write_bytes(b"")
            Path(directory, "a").write_bytes(b"")
            os.chmod(Path(directory, "z"), 0o640)
            os.chmod(Path(directory, "a"), 0o600)
            local = LocalDockerContext("FROM scratch", context_dir=directory)
            self.assertEqual(
                list(local.walk()),
                [
                    DockerContextEntry("a", "file", 0o600),
                    DockerContextEntry("z", "file", 0o640),
                ],
            )
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

    def test_local_walk_fails_closed_when_descending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            blocked = Path(directory, "blocked")
            blocked.mkdir()
            (blocked / "required.txt").write_text("required", encoding="utf-8")
            local = LocalDockerContext("FROM scratch", context_dir=directory)
            real_scandir = os.scandir

            def deny_blocked(path: str) -> object:
                if os.path.abspath(os.fspath(path)) == str(blocked):
                    raise PermissionError(
                        errno.EACCES, "Permission denied", str(blocked)
                    )
                return real_scandir(path)

            with (
                patch(
                    "akernel_sdk._dockercontext.os.scandir",
                    side_effect=deny_blocked,
                ),
                self.assertRaisesRegex(DockerContextError, "blocked") as raised,
            ):
                list(local.walk())
            self.assertIsInstance(raised.exception.__cause__, PermissionError)

            with (
                patch(
                    "akernel_sdk._dockercontext.os.scandir",
                    side_effect=deny_blocked,
                ),
                self.assertRaisesRegex(
                    DockerContextError, "failed to walk Docker context"
                ) as raised,
            ):
                _ContextManifest.from_context(local)
            walk_error = raised.exception.__cause__
            self.assertIsInstance(walk_error, DockerContextError)
            self.assertIn("blocked", str(walk_error))
            self.assertIsInstance(walk_error.__cause__, PermissionError)

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

    def test_entries_preserve_empty_directories_and_modes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            empty = root / "empty"
            nested = empty / "nested"
            nested.mkdir(parents=True)
            executable = root / "run.sh"
            executable.write_bytes(b"#!/bin/sh\n")
            os.chmod(empty, 0o711)
            os.chmod(nested, 0o750)
            os.chmod(executable, 0o755)

            self.assertEqual(
                list(LocalDockerContext("FROM scratch", context_dir=root).walk()),
                [
                    DockerContextEntry("empty", "directory", 0o711),
                    DockerContextEntry("empty/nested", "directory", 0o750),
                    DockerContextEntry("run.sh", "file", 0o755),
                ],
            )

    def test_dockerignore_filters_empty_directories(self) -> None:
        entries = [
            DockerContextEntry(".dockerignore", "file", 0o644),
            DockerContextEntry("ignored", "directory", 0o755),
            DockerContextEntry("visible", "directory", 0o711),
            DockerContextEntry("visible/nested", "directory", 0o750),
        ]
        context = MemoryDockerContext({".dockerignore": b"ignored/" + NL}, entries)
        manifest = _ContextManifest.from_context(context)
        self.assertEqual(
            [
                (entry.source_path, entry.relative_target)
                for entry in manifest.select(".").entries
            ],
            [
                (".dockerignore", ".dockerignore"),
                ("visible", "visible"),
                ("visible/nested", "visible/nested"),
            ],
        )
        with self.assertRaisesRegex(DockerContextError, "ignored"):
            manifest.select("ignored")

    def test_context_entry_rejects_invalid_mode(self) -> None:
        class MaliciousMode(int):
            def __format__(self, format_spec: str) -> str:
                return "0000; injected"

        for mode in (-1, 0o1000, True, MaliciousMode(0o644)):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(ValueError, "permission bits"):
                    DockerContextEntry("path", "file", mode)

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
