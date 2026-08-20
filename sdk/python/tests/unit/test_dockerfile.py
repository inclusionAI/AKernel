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

"""Unit tests for the Dockerfile sandbox-launch path (RFC §8)."""

from __future__ import annotations

import errno
import io
import os
import tarfile
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO
from unittest.mock import patch

from akernel_sdk._dockercontext import (
    DockerContext,
    DockerContextEntry,
    DockerContextError,
    LocalDockerContext,
)
from akernel_sdk._dockerfile import (
    DIRECT_LAUNCH_ROOTFS_ONLY_WARNING,
    CmdInstruction,
    CopyInstruction,
    DockerfileBuildError,
    DockerfileLaunch,
    DockerfileParseError,
    RunInstruction,
    UserInstruction,
    _json_array,
    check_direct_launch,
    parse_dockerfile,
)
from akernel_sdk._dockerfile_runner import (
    _resolve_start_cmd,
    _Runner,
    apply_dockerfile,
    wrap_user,
)


class _MockResult:
    def __init__(self, exit_code=0, stdout="", stderr=""):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class _MockHandle:
    pass


class _MockFiles:
    def __init__(self, existing_paths: set[str] | None = None):
        self.ops: list[tuple] = []
        self.existing_paths = existing_paths or set()
        self.exists_calls: list[str] = []

    def exists(self, path):
        self.exists_calls.append(path)
        return path in self.existing_paths

    def copy_from_local(self, local, remote):
        self.ops.append(("cp", local, remote))

    def make_dir(self, path):
        self.ops.append(("mkdir", path))
        return True


class _MockCommands:
    def __init__(self):
        self.ops: list[tuple] = []

    def run(self, cmd, background=False, envs=None, cwd=None, timeout=60, stdin=False):
        self.ops.append(("run", cmd, background, envs, cwd, timeout))
        return _MockResult()


class _MockSandbox:
    def __init__(
        self,
        failing_cmd=None,
        startup_error=None,
        existing_paths: set[str] | None = None,
    ):
        self.files = _MockFiles(existing_paths)
        self.commands = _MockCommands(
            failing=failing_cmd,
            startup_error=startup_error,
        )
        self._running = True
        self.is_running_calls = 0

    def is_running(self):
        self.is_running_calls += 1
        return self._running


class _MockCommands(_MockCommands):
    def __init__(self, failing=None, startup_error=None):
        super().__init__()
        self._failing = failing
        self._startup_error = startup_error
        self.startup_handle = _MockHandle()

    def run(self, cmd, background=False, envs=None, cwd=None, timeout=60, stdin=False):
        self.ops.append(("run", cmd, background, envs, cwd, timeout))
        if background:
            if self._startup_error is not None:
                raise self._startup_error
            return self.startup_handle
        if self._failing and self._failing in cmd:
            return _MockResult(exit_code=1, stderr=f"boom at {self._failing}")
        return _MockResult()


def _archive_bytes(
    members: list[tuple[tarfile.TarInfo, bytes | None]],
) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for info, content in members:
            archive.addfile(info, io.BytesIO(content) if content is not None else None)
    return output.getvalue()


def _regular_member(name: str, content: bytes = b"x") -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(content)
    return info, content


class _MemoryDockerContext(DockerContext):
    def __init__(
        self,
        dockerfile: str,
        files: dict[str, bytes],
        *,
        fail_path: str | None = None,
        entries: tuple[DockerContextEntry, ...] | None = None,
    ) -> None:
        self._dockerfile = dockerfile
        self._files = files
        self._fail_path = fail_path
        self._entries = entries
        self.open_paths: list[str] = []

    def dockerfile_text(self) -> str:
        return self._dockerfile

    @contextmanager
    def open(self, path: str) -> Iterator[BinaryIO]:
        self.open_paths.append(path)
        if path == self._fail_path:
            raise OSError(f"cannot open {path}")
        yield io.BytesIO(self._files[path])

    def walk(self) -> Iterator[DockerContextEntry]:
        if self._entries is not None:
            yield from self._entries
            return
        directories = {
            "/".join(path.split("/")[:index])
            for path in self._files
            for index in range(1, len(path.split("/")))
        }
        yield from (
            DockerContextEntry(path, "directory", 0o755)
            for path in sorted(directories)
        )
        yield from (
            DockerContextEntry(path, "file", 0o644)
            for path in sorted(self._files)
        )


class TestDockerfileLaunch(unittest.TestCase):
    def test_defaults_and_custom_values(self):
        context = LocalDockerContext("FROM ubuntu\n")
        defaults = DockerfileLaunch(context)
        self.assertIs(defaults.context, context)
        self.assertTrue(defaults.auto_start_cmd)
        self.assertEqual(defaults.run_timeout, 600)

        custom = DockerfileLaunch(
            context,
            auto_start_cmd=False,
            run_timeout=300,
        )
        self.assertFalse(custom.auto_start_cmd)
        self.assertEqual(custom.run_timeout, 300)

    def test_is_frozen(self):
        launch = DockerfileLaunch(LocalDockerContext("FROM ubuntu\n"))
        with self.assertRaisesRegex(AttributeError, "cannot assign"):
            launch.run_timeout = 300  # type: ignore[misc]

    def test_rejects_invalid_values(self):
        context = LocalDockerContext("FROM ubuntu\n")
        with self.assertRaisesRegex(TypeError, "context"):
            DockerfileLaunch(object())  # type: ignore[arg-type]
        with self.assertRaisesRegex(TypeError, "auto_start_cmd"):
            DockerfileLaunch(context, auto_start_cmd=1)  # type: ignore[arg-type]
        for timeout in (True, 1.5):
            with self.subTest(timeout=timeout):
                with self.assertRaisesRegex(TypeError, "run_timeout"):
                    DockerfileLaunch(context, run_timeout=timeout)  # type: ignore[arg-type]
        for timeout in (0, -1):
            with self.subTest(timeout=timeout):
                with self.assertRaisesRegex(ValueError, "run_timeout"):
                    DockerfileLaunch(context, run_timeout=timeout)


class TestParseDockerfile(unittest.TestCase):
    def _parse(self, content, strict=False):
        return parse_dockerfile(LocalDockerContext(content), strict=strict)

    def test_basic_instructions(self):
        parsed = self._parse(
            "FROM ubuntu:22.04\n"
            "RUN apt-get update\n"
            "COPY app.py /app/\n"
            "ENV K=v\n"
            "WORKDIR /srv\n"
            "USER app\n"
            'CMD ["python3", "app.py"]\n'
        )
        self.assertEqual(parsed.base_image, "ubuntu:22.04")
        self.assertEqual(parsed.envs, {"K": "v"})
        self.assertEqual(parsed.workdir, "/srv")
        self.assertEqual(parsed.user, "app")
        self.assertEqual(parsed.start_cmd, ("python3", "app.py"))

    def test_from_alias_unaffected(self):
        parsed = self._parse("FROM node:20 AS builder\nRUN echo hi\n")
        self.assertEqual(parsed.base_image, "node:20")

    def test_multi_stage_rejected(self):
        with self.assertRaisesRegex(DockerfileParseError, "Multi-stage"):
            self._parse("FROM ubuntu AS a\nFROM node:20\n")

    def test_missing_from(self):
        with self.assertRaisesRegex(DockerfileParseError, "FROM"):
            self._parse("RUN echo hi\n")

    def test_env_double_form(self):
        parsed = self._parse(
            "FROM ubuntu\nENV K1=v1 K2=v2\nENV SINGLE some value here\n"
        )
        self.assertEqual(
            parsed.envs,
            {"K1": "v1", "K2": "v2", "SINGLE": "some value here"},
        )

    def test_copy_chown_parsed(self):
        parsed = self._parse("FROM ubuntu\nCOPY --chown=app:app src.py /app/\n")
        ins = [i for i in parsed.instructions if isinstance(i, CopyInstruction)][0]
        self.assertEqual(ins.chown, "app:app")
        self.assertEqual(ins.srcs, ("src.py",))
        self.assertEqual(ins.dest, "/app/")

    def test_cmd_shell_form(self):
        parsed = self._parse("FROM ubuntu\nCMD python3 app.py\n")
        ins = [i for i in parsed.instructions if isinstance(i, CmdInstruction)][0]
        self.assertTrue(ins.shell_form)

    def test_parsed_start_cmd_merged_with_entrypoint(self):
        parsed = self._parse('FROM ubuntu\nENTRYPOINT ["python3"]\nCMD ["app.py"]\n')
        self.assertEqual(parsed.start_cmd, ("python3", "app.py"))

    def test_comment_skipped(self):
        parsed = self._parse("FROM ubuntu:22.04\n# a comment\nRUN echo hi\n")
        self.assertEqual(parsed.unsupported, ())
        self.assertFalse(any("COMMENT" in warning for warning in parsed.warnings))
        result = check_direct_launch(
            LocalDockerContext("FROM ubuntu:22.04\n# a comment\nRUN echo hi\n")
        )
        self.assertNotIn("COMMENT", result.ignored_instructions)

    def test_copy_multiple_sources(self):
        parsed = self._parse("FROM ubuntu\nCOPY a.py b.py /app/\n")
        ins = [i for i in parsed.instructions if isinstance(i, CopyInstruction)][0]
        self.assertEqual(ins.srcs, ("a.py", "b.py"))
        self.assertEqual(ins.dest, "/app/")

    def test_run_leading_flags_fail_closed(self):
        for command in (
            "--mount=type=secret,id=x; touch /tmp/x",
            "--network=none echo x",
            "--security=insecure echo x",
            "--device=nvidia.com/gpu=all echo x",
            "--future-flag=enabled echo x",
        ):
            with self.subTest(command=command):
                dockerfile = f"FROM ubuntu\nRUN {command}\n"
                parsed = self._parse(dockerfile)
                self.assertEqual(parsed.unsupported[0].reason, "unsupported_syntax")
                self.assertFalse(
                    any(
                        isinstance(item, RunInstruction) for item in parsed.instructions
                    )
                )
                self.assertFalse(
                    check_direct_launch(
                        LocalDockerContext(dockerfile)
                    ).direct_launchable
                )
                with self.assertRaises(DockerfileParseError):
                    self._parse(dockerfile, strict=True)

    def test_run_nonleading_double_dash_and_bracket_command_are_supported(self):
        parsed = self._parse(
            "FROM ubuntu\nRUN printf -- '%s' x\nRUN [ -f /x ]\n", strict=True
        )
        commands = [
            item.command
            for item in parsed.instructions
            if isinstance(item, RunInstruction)
        ]
        self.assertEqual(commands, ["printf -- '%s' x", "[ -f /x ]"])

    def test_run_malformed_quoting_fails_closed(self):
        dockerfile = 'FROM ubuntu\nRUN echo "unterminated\n'
        parsed = self._parse(dockerfile)
        self.assertEqual(parsed.unsupported[0].kind, "RUN")
        self.assertFalse(
            any(isinstance(item, RunInstruction) for item in parsed.instructions)
        )
        with self.assertRaisesRegex(DockerfileParseError, "malformed quoting"):
            self._parse(dockerfile, strict=True)


class TestCheckDirectLaunch(unittest.TestCase):
    def test_simple_direct_launchable(self):
        r = check_direct_launch(LocalDockerContext("FROM ubuntu:22.04\nRUN echo hi\n"))
        self.assertTrue(r.direct_launchable)
        self.assertEqual(r.reasons, ())
        self.assertTrue(r.has_build_instructions)
        self.assertEqual(r.base_image, "ubuntu:22.04")
        self.assertIn(DIRECT_LAUNCH_ROOTFS_ONLY_WARNING, r.warnings)

    def test_no_build_instructions(self):
        r = check_direct_launch(LocalDockerContext("FROM ubuntu\nENV K=v\n"))
        self.assertTrue(r.direct_launchable)
        self.assertFalse(r.has_build_instructions)

    def test_check_multi_stage_reason_unchanged(self):
        r = check_direct_launch(LocalDockerContext("FROM ubuntu AS a\nFROM node:20\n"))
        self.assertFalse(r.direct_launchable)
        self.assertEqual(r.reasons, ("multi_stage",))

    def test_no_from_not_launchable(self):
        r = check_direct_launch(LocalDockerContext("RUN echo hi\n"))
        self.assertFalse(r.direct_launchable)
        self.assertEqual(r.reasons, ("no_from",))

    def test_check_direct_launch_no_comment_warning(self):
        r = check_direct_launch(
            LocalDockerContext("FROM ubuntu:22.04\n# a comment\nRUN echo hi\n")
        )
        self.assertEqual(r.ignored_instructions, ())
        self.assertFalse(any("COMMENT" in warning for warning in r.warnings))

    def test_run_does_not_break_launchable(self):
        r = check_direct_launch(
            LocalDockerContext("FROM ubuntu\nRUN apt-get install -y curl\nCOPY x /y\n")
        )
        self.assertTrue(r.direct_launchable)
        self.assertTrue(r.has_build_instructions)

    def test_warnings_mention_no_snapshot(self):
        r = check_direct_launch(LocalDockerContext("FROM ubuntu\nRUN echo hi\n"))
        self.assertTrue(any("no snapshot" in w for w in r.warnings))


class TestWrapUser(unittest.TestCase):
    def test_no_user_and_root_passthrough(self):
        self.assertEqual(wrap_user("whoami", None), "whoami")
        self.assertEqual(wrap_user("whoami", "root"), "whoami")

    def test_user_wraps_runuser_with_su_fallback(self):
        wrapped = wrap_user("whoami", "app")
        self.assertIn("runuser", wrapped)
        self.assertIn("su -s /bin/sh", wrapped)
        self.assertIn("app", wrapped)

    def test_unsupported_user_is_not_silently_rewritten(self):
        for user in ("app:grp", "0", "1000:1001", ""):
            with self.subTest(user=user):
                with self.assertRaisesRegex(ValueError, "named user"):
                    wrap_user("whoami", user)


class TestResolveStartCmd(unittest.TestCase):
    def _parse(self, content):
        return parse_dockerfile(LocalDockerContext(content))

    def test_cmd_only(self):
        parsed = self._parse('FROM ubuntu\nCMD ["a", "b"]\n')
        self.assertEqual(_resolve_start_cmd(parsed), ("a", "b"))

    def test_entrypoint_only(self):
        parsed = self._parse('FROM ubuntu\nENTRYPOINT ["python3"]\n')
        self.assertEqual(_resolve_start_cmd(parsed), ("python3",))

    def test_entrypoint_and_cmd_concat(self):
        parsed = self._parse('FROM ubuntu\nENTRYPOINT ["python3"]\nCMD ["app.py"]\n')
        self.assertEqual(_resolve_start_cmd(parsed), ("python3", "app.py"))

    def test_no_cmd_no_entrypoint(self):
        parsed = self._parse("FROM ubuntu\nRUN echo hi\n")
        self.assertIsNone(_resolve_start_cmd(parsed))


class TestApplyDockerfile(unittest.TestCase):
    def _apply(self, dockerfile, sb, auto_start_cmd=False, run_timeout=60):
        ctx = LocalDockerContext(dockerfile)
        parsed = parse_dockerfile(ctx)
        return apply_dockerfile(
            sb,
            parsed,
            ctx,
            auto_start_cmd=auto_start_cmd,
            run_timeout=run_timeout,
        )

    def test_run_uses_accumulated_envs_cwd_user(self):
        sb = _MockSandbox()
        self._apply(
            "FROM ubuntu\nENV K=v\nWORKDIR /app\nUSER app\nRUN whoami\n",
            sb,
            auto_start_cmd=False,
        )
        run_ops = [o for o in sb.commands.ops if o[0] == "run"]
        # whoami should be wrapped with runuser + carry envs and cwd
        last_run = run_ops[-1]
        self.assertIn("runuser", last_run[1])
        self.assertIn("app", last_run[1])
        self.assertEqual(last_run[3], {"K": "v"})  # envs
        self.assertEqual(last_run[4], "/app")  # cwd
        # make_dir called for WORKDIR
        self.assertIn(("mkdir", "/app"), sb.files.ops)

    def test_run_failure_raises_build_error(self):
        sb = _MockSandbox(failing_cmd="whoami")
        with self.assertRaises(DockerfileBuildError) as cm:
            self._apply("FROM ubuntu\nRUN whoami\n", sb, auto_start_cmd=False)
        self.assertIn("exit code", str(cm.exception))

    def test_auto_start_launches_background(self):
        sb = _MockSandbox()
        res = self._apply(
            'FROM ubuntu\nRUN echo hi\nCMD ["python3", "app.py"]\n',
            sb,
            auto_start_cmd=True,
        )
        bg_ops = [o for o in sb.commands.ops if o[0] == "run" and o[2] is True]
        self.assertEqual(len(bg_ops), 1)
        self.assertEqual(res.start_cmd, ("python3", "app.py"))
        self.assertIs(res.startup_command, sb.commands.startup_handle)
        self.assertEqual(sb.is_running_calls, 1)

    def test_no_auto_start_returns_cmd(self):
        sb = _MockSandbox()
        res = self._apply(
            'FROM ubuntu\nRUN echo hi\nCMD ["python3", "app.py"]\n',
            sb,
            auto_start_cmd=False,
        )
        bg_ops = [o for o in sb.commands.ops if o[0] == "run" and o[2] is True]
        self.assertEqual(len(bg_ops), 0)
        self.assertEqual(res.start_cmd, ("python3", "app.py"))
        self.assertIsNone(res.startup_command)

    def test_no_start_command_returns_no_startup_handle(self):
        sb = _MockSandbox()
        res = self._apply("FROM ubuntu\nRUN echo hi\n", sb, auto_start_cmd=True)
        self.assertIsNone(res.start_cmd)
        self.assertIsNone(res.startup_command)
        self.assertEqual(sb.is_running_calls, 0)

    def test_startup_dispatch_failure_includes_instruction_metadata(self):
        startup_error = RuntimeError("start RPC failed")
        sb = _MockSandbox(startup_error=startup_error)
        with self.assertRaises(DockerfileBuildError) as raised:
            self._apply('FROM ubuntu\nCMD ["server"]\n', sb, auto_start_cmd=True)
        self.assertEqual(raised.exception.instruction, "CMD")
        self.assertIs(raised.exception.__cause__, startup_error)

    def test_workdir_make_dir(self):
        sb = _MockSandbox()
        self._apply("FROM ubuntu\nWORKDIR /srv/app\nRUN pwd\n", sb)
        self.assertIn(("mkdir", "/srv/app"), sb.files.ops)

    def test_copy_chown_runs_chown(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "app.py"), "w") as _f:
                _f.write("print(1)")
            sb = _MockSandbox()
            ctx = LocalDockerContext(
                "FROM ubuntu\nCOPY --chown=app:app app.py /app/\n",
                context_dir=d,
            )
            parsed = parse_dockerfile(ctx)
            apply_dockerfile(sb, parsed, ctx, auto_start_cmd=False)
            # Should have: copy_from_local + a chown command run
            cp_ops = [o for o in sb.files.ops if o[0] == "cp"]
            self.assertTrue(cp_ops)
            run_ops = [o for o in sb.commands.ops if o[0] == "run"]
            chown_ops = [o for o in run_ops if "chown" in o[1]]
            self.assertTrue(chown_ops)
            self.assertIn("chown app:app /app/app.py", chown_ops[0][1])

    def test_copy_to_dir_dest_appends_basename(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "app.py"), "w") as _f:
                _f.write("print(1)")
            sb = _MockSandbox()
            ctx = LocalDockerContext("FROM ubuntu\nCOPY app.py /srv/\n", context_dir=d)
            parsed = parse_dockerfile(ctx)
            apply_dockerfile(sb, parsed, ctx, auto_start_cmd=False)
            cp_ops = [o for o in sb.files.ops if o[0] == "cp"]
            # dir-form dest (trailing /) places the file inside by basename
            self.assertEqual(cp_ops[-1][2], "/srv/app.py")

    def test_add_tar_creates_dest_dir_before_extract(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            import tarfile

            archive = os.path.join(d, "app.tar.gz")
            with tarfile.open(archive, "w:gz") as tar:
                inner = os.path.join(d, "top.txt")
                with open(inner, "w") as _f:
                    _f.write("x")
                tar.add(inner, arcname="top.txt")
            sb = _MockSandbox()
            ctx = LocalDockerContext(
                "FROM ubuntu\nUSER app\nADD app.tar.gz /opt/app/\n",
                context_dir=d,
            )
            parsed = parse_dockerfile(ctx)
            apply_dockerfile(sb, parsed, ctx, auto_start_cmd=False)
            # dest dir created before extraction
            self.assertTrue(
                any(
                    o[0] == "mkdir" and o[1].rstrip("/") == "/opt/app"
                    for o in sb.files.ops
                ),
                sb.files.ops,
            )
            # tar extraction command was issued
            run_ops = [o for o in sb.commands.ops if o[0] == "run"]
            self.assertTrue(
                any("tar xf" in o[1] and "/opt/app" in o[1] for o in run_ops)
            )
            self.assertTrue(any("--no-same-owner" in o[1] for o in run_ops))
            tar_op = next(o for o in run_ops if "tar xf" in o[1])
            self.assertNotIn("runuser", tar_op[1])
            self.assertNotIn("su -s", tar_op[1])
            self.assertEqual(tar_op[4], "/")

    def test_add_tar_rejects_path_traversal(self):
        import io
        import os
        import tarfile
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            archive = os.path.join(d, "unsafe.tar")
            with tarfile.open(archive, "w") as tar:
                content = b"evil"
                info = tarfile.TarInfo("../evil.txt")
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
            sb = _MockSandbox()
            ctx = LocalDockerContext(
                "FROM ubuntu\nADD unsafe.tar /opt/app/\n", context_dir=d
            )
            parsed = parse_dockerfile(ctx)
            with self.assertRaisesRegex(DockerfileBuildError, "unsafe"):
                apply_dockerfile(sb, parsed, ctx, auto_start_cmd=False)
            self.assertEqual(sb.files.ops, [])
            self.assertEqual(sb.commands.ops, [])


class TestDockerfileManifestCopies(unittest.TestCase):
    def _apply_memory(
        self,
        dockerfile: str,
        files: dict[str, bytes],
        *,
        fail_path: str | None = None,
        existing_paths: set[str] | None = None,
        entries: tuple[DockerContextEntry, ...] | None = None,
    ) -> tuple[_MockSandbox, _MemoryDockerContext]:
        context = _MemoryDockerContext(
            dockerfile, files, fail_path=fail_path, entries=entries
        )
        sandbox = _MockSandbox(existing_paths=existing_paths)
        apply_dockerfile(
            sandbox,
            parse_dockerfile(context),
            context,
            auto_start_cmd=False,
        )
        return sandbox, context

    def test_dot_honors_dockerignore_and_copies_visible_control_files(self) -> None:
        files = {
            ".dockerignore": b".env\n.git/**\n",
            "Dockerfile": b"FROM scratch\n",
            ".env": b"secret",
            ".git/config": b"git",
            "allowed": b"ok",
            "sub/x": b"x",
        }
        sandbox, context = self._apply_memory("FROM ubuntu\nCOPY . /app/\n", files)
        copies = [
            operation[2] for operation in sandbox.files.ops if operation[0] == "cp"
        ]
        self.assertEqual(
            copies,
            ["/app/.dockerignore", "/app/Dockerfile", "/app/allowed", "/app/sub/x"],
        )
        self.assertEqual(
            context.open_paths,
            [".dockerignore", ".dockerignore", "Dockerfile", "allowed", "sub/x"],
        )

    def test_reincluded_dockerignore_descendant_copies_from_virtual_dir(self) -> None:
        files = {
            ".dockerignore": b"docs\n!docs/README.md\n",
            "docs/README.md": b"visible",
            "docs/private.txt": b"hidden",
        }
        for source in ("docs", "*"):
            with self.subTest(source=source):
                sandbox, context = self._apply_memory(
                    f"FROM ubuntu\nCOPY {source} /out/\n", files
                )
                copies = [
                    operation[2]
                    for operation in sandbox.files.ops
                    if operation[0] == "cp"
                ]
                expected = ["/out/README.md"]
                opened = [".dockerignore", "docs/README.md"]
                if source == "*":
                    expected.insert(0, "/out/.dockerignore")
                    opened.insert(1, ".dockerignore")
                self.assertEqual(copies, expected)
                self.assertEqual(context.open_paths, opened)

    def test_local_and_memory_contexts_have_same_copy_targets(self) -> None:
        import os
        import tempfile

        dockerfile = "FROM ubuntu\nCOPY src /app\n"
        memory_sandbox, _ = self._apply_memory(
            dockerfile, {"src/a.py": b"a", "src/sub/b.py": b"b"}
        )
        with tempfile.TemporaryDirectory() as directory:
            os.makedirs(os.path.join(directory, "src", "sub"))
            with open(os.path.join(directory, "src", "a.py"), "wb") as output:
                output.write(b"a")
            with open(os.path.join(directory, "src", "sub", "b.py"), "wb") as output:
                output.write(b"b")
            context = LocalDockerContext(dockerfile, context_dir=directory)
            local_sandbox = _MockSandbox()
            apply_dockerfile(
                local_sandbox,
                parse_dockerfile(context),
                context,
                auto_start_cmd=False,
            )
        memory_targets = [op[2] for op in memory_sandbox.files.ops if op[0] == "cp"]
        local_targets = [op[2] for op in local_sandbox.files.ops if op[0] == "cp"]
        self.assertEqual(local_targets, memory_targets)

    def test_literal_directory_dot_and_wildcard_targets(self) -> None:
        files = {
            "root.py": b"root",
            "src/a.py": b"a",
            "src/sub/b.py": b"b",
            "other.txt": b"other",
        }
        cases = (
            ("COPY src/a.py /one/\n", ["/one/a.py"]),
            ("COPY src /two\n", ["/two/a.py", "/two/sub/b.py"]),
            (
                "COPY . /three/\n",
                [
                    "/three/other.txt",
                    "/three/root.py",
                    "/three/src/a.py",
                    "/three/src/sub/b.py",
                ],
            ),
            ("COPY *.py /four/\n", ["/four/root.py"]),
            ("COPY src/**/*.py /five/\n", ["/five/b.py"]),
        )
        for instruction, expected in cases:
            with self.subTest(instruction=instruction):
                sandbox, _ = self._apply_memory("FROM ubuntu\n" + instruction, files)
                copies = [op[2] for op in sandbox.files.ops if op[0] == "cp"]
                self.assertEqual(copies, expected)

    def test_wildcard_directory_copy_targets_match_buildkit(self) -> None:
        for destination in ("/subdest/", "/subdest"):
            with self.subTest(destination=destination):
                sandbox, _ = self._apply_memory(
                    f"FROM ubuntu\nCOPY sub/* {destination}\n",
                    {"sub/dir1/dir2/foo": b"foo"},
                )
                self.assertEqual(
                    [
                        operation[2]
                        for operation in sandbox.files.ops
                        if operation[0] == "cp"
                    ],
                    ["/subdest/dir2/foo"],
                )
                made_dirs = [
                    operation[1]
                    for operation in sandbox.files.ops
                    if operation[0] == "mkdir"
                ]
                self.assertEqual(made_dirs, ["/subdest", "/subdest/dir2"])
                self.assertNotIn("/subdest/dir1", made_dirs)

        mixed, _ = self._apply_memory(
            "FROM ubuntu\nCOPY sub/* /subdest/\n",
            {"sub/dir1/dir2/foo": b"foo", "sub/file": b"file"},
        )
        self.assertEqual(
            [operation[2] for operation in mixed.files.ops if operation[0] == "cp"],
            ["/subdest/dir2/foo", "/subdest/file"],
        )

        modules, _ = self._apply_memory(
            "FROM ubuntu\nCOPY modules/** /dest/\n",
            {"modules/one/x.py": b"x", "modules/two/y.txt": b"y"},
        )
        self.assertEqual(
            [
                operation[2]
                for operation in modules.files.ops
                if operation[0] == "cp"
            ],
            ["/dest/x.py", "/dest/y.txt"],
        )
        module_directories = [
            operation[1]
            for operation in modules.files.ops
            if operation[0] == "mkdir"
        ]
        self.assertEqual(module_directories, ["/dest"])

        multiple, _ = self._apply_memory(
            "FROM ubuntu\nCOPY * /target/\n",
            {"one/a": b"a", "two/b": b"b"},
        )
        self.assertEqual(
            [
                operation[2]
                for operation in multiple.files.ops
                if operation[0] == "cp"
            ],
            ["/target/a", "/target/b"],
        )

        empty = _MemoryDockerContext(
            "FROM ubuntu\nCOPY --chown=app:app empty* /target/\n",
            {},
            entries=(DockerContextEntry("empty", "directory", 0o700),),
        )
        empty_sandbox = _MockSandbox()
        apply_dockerfile(
            empty_sandbox,
            parse_dockerfile(empty),
            empty,
            auto_start_cmd=False,
        )
        self.assertEqual(empty_sandbox.files.ops, [("mkdir", "/target")])
        self.assertEqual(
            [op[1] for op in empty_sandbox.commands.ops if "chown" in op[1]],
            ["chown app:app /target"],
        )

        root_empty = _MemoryDockerContext(
            "FROM ubuntu\nCOPY --chown=app:app empty* /\n",
            {},
            entries=(DockerContextEntry("empty", "directory", 0o700),),
        )
        root_empty_sandbox = _MockSandbox()
        apply_dockerfile(
            root_empty_sandbox,
            parse_dockerfile(root_empty),
            root_empty,
            auto_start_cmd=False,
        )
        self.assertEqual(root_empty_sandbox.files.ops, [])
        self.assertEqual(root_empty_sandbox.files.exists_calls, [])
        self.assertEqual(root_empty_sandbox.commands.ops, [])

        collision = _MemoryDockerContext(
            "FROM ubuntu\nCOPY * /target/\n",
            {"left/same": b"left", "right/same": b"right"},
        )
        collision_sandbox = _MockSandbox()
        with self.assertRaisesRegex(DockerfileBuildError, "colliding"):
            apply_dockerfile(
                collision_sandbox,
                parse_dockerfile(collision),
                collision,
                auto_start_cmd=False,
            )
        self.assertEqual(collision_sandbox.files.ops, [])
        self.assertEqual(collision_sandbox.commands.ops, [])

    def test_wildcard_multiple_sources_require_directory_destination(self) -> None:
        rejected_cases = (
            ("two directories", {"one/a": b"a", "two/b": b"b"}),
            ("two files", {"one": b"one", "two": b"two"}),
            ("directory and file", {"dir/a": b"a", "file": b"file"}),
        )
        for name, files in rejected_cases:
            with self.subTest(name=name):
                context = _MemoryDockerContext("FROM ubuntu\nCOPY * /target\n", files)
                sandbox = _MockSandbox()
                with self.assertRaisesRegex(DockerfileBuildError, "multiple sources"):
                    apply_dockerfile(
                        sandbox,
                        parse_dockerfile(context),
                        context,
                        auto_start_cmd=False,
                    )
                self.assertEqual(sandbox.files.ops, [])
                self.assertEqual(sandbox.commands.ops, [])
                self.assertEqual(context.open_paths, [])

        valid_cases = (
            (
                "two directories",
                {"one/a": b"a", "two/b": b"b"},
                ["/target/a", "/target/b"],
            ),
            (
                "two files",
                {"one": b"one", "two": b"two"},
                ["/target/one", "/target/two"],
            ),
            (
                "directory and file",
                {"dir/a": b"a", "file": b"file"},
                ["/target/a", "/target/file"],
            ),
        )
        for name, files, targets in valid_cases:
            with self.subTest(name=name):
                sandbox, _ = self._apply_memory(
                    "FROM ubuntu\nCOPY * /target/\n", files
                )
                self.assertEqual(
                    [
                        operation[2]
                        for operation in sandbox.files.ops
                        if operation[0] == "cp"
                    ],
                    targets,
                )

        single_directory, _ = self._apply_memory(
            "FROM ubuntu\nCOPY * /target\n", {"one/a": b"a"}
        )
        self.assertEqual(
            [
                operation[2]
                for operation in single_directory.files.ops
                if operation[0] == "cp"
            ],
            ["/target/a"],
        )
        single_file, _ = self._apply_memory(
            "FROM ubuntu\nCOPY * /target\n", {"one": b"one"}
        )
        self.assertEqual(
            [
                operation[2]
                for operation in single_file.files.ops
                if operation[0] == "cp"
            ],
            ["/target"],
        )

    def test_local_traversal_failure_prevents_copy_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            blocked = Path(directory, "blocked")
            blocked.mkdir()
            (blocked / "required.txt").write_text("required", encoding="utf-8")
            context = LocalDockerContext(
                "FROM ubuntu\nCOPY blocked /app\n", context_dir=directory
            )
            sandbox = _MockSandbox()
            real_scandir = os.scandir

            def deny_blocked(path: str | int) -> object:
                if (
                    isinstance(path, (str, bytes, os.PathLike))
                    and os.path.abspath(os.fspath(path)) == str(blocked)
                ):
                    raise PermissionError(
                        errno.EACCES, "Permission denied", str(blocked)
                    )
                return real_scandir(path)

            with (
                patch(
                    "akernel_sdk._dockercontext.os.scandir",
                    side_effect=deny_blocked,
                ),
                self.assertRaisesRegex(DockerfileBuildError, "blocked"),
            ):
                apply_dockerfile(
                    sandbox,
                    parse_dockerfile(context),
                    context,
                    auto_start_cmd=False,
                )
            self.assertEqual(sandbox.files.ops, [])
            self.assertEqual(sandbox.commands.ops, [])

    def test_selection_and_target_errors_have_no_sandbox_operations(self) -> None:
        cases = (
            ("COPY no-match /dest/\n", {"ok": b""}),
            (
                "COPY hidden.py /dest/\n",
                {".dockerignore": b"hidden.py\n", "hidden.py": b""},
            ),
            ("COPY /absolute /dest/\n", {"absolute": b""}),
            ("COPY ../parent /dest/\n", {"parent": b""}),
            ("COPY *.py /dest\n", {"a.py": b"", "b.py": b""}),
            ("COPY file[.txt /dest/\n", {}),
            ("COPY one/a two/a /dest/\n", {"one/a": b"", "two/a": b""}),
            ("COPY . /dest/\n", {}),
            (
                "COPY . /dest/\n",
                {".dockerignore": b"*\n", "hidden.txt": b""},
            ),
        )
        for instruction, files in cases:
            with self.subTest(instruction=instruction):
                context = _MemoryDockerContext("FROM ubuntu\n" + instruction, files)
                sandbox = _MockSandbox()
                with self.assertRaises(DockerfileBuildError) as raised:
                    apply_dockerfile(
                        sandbox,
                        parse_dockerfile(context),
                        context,
                        auto_start_cmd=False,
                    )
                self.assertEqual(raised.exception.index, 1)
                self.assertEqual(raised.exception.instruction, "COPY")
                self.assertEqual(sandbox.files.ops, [])
                self.assertEqual(sandbox.commands.ops, [])
                if ".dockerignore" not in files:
                    self.assertEqual(context.open_paths, [])

    def test_empty_copy_plan_has_no_sandbox_operations(self) -> None:
        sandbox = _MockSandbox()
        runner = _Runner(
            sandbox,
            _MemoryDockerContext("FROM ubuntu\n", {"visible": b""}),
            run_timeout=60,
        )
        with self.assertRaisesRegex(DockerfileBuildError, "select no files"):
            runner._validate_copy_plan([], index=1, instruction="COPY")
        self.assertEqual(sandbox.files.ops, [])
        self.assertEqual(sandbox.commands.ops, [])

    def test_context_open_failure_has_no_sandbox_operations(self) -> None:
        context = _MemoryDockerContext(
            "FROM ubuntu\nCOPY a b /dest/\n",
            {"a": b"a", "b": b"b"},
            fail_path="b",
        )
        sandbox = _MockSandbox()
        with self.assertRaisesRegex(
            DockerfileBuildError, "source 'b'.*cannot open"
        ) as raised:
            apply_dockerfile(
                sandbox, parse_dockerfile(context), context, auto_start_cmd=False
            )
        self.assertEqual(raised.exception.index, 1)
        self.assertEqual(raised.exception.instruction, "COPY")
        self.assertEqual(context.open_paths, ["a", "b"])
        self.assertEqual(sandbox.files.ops, [])
        self.assertEqual(sandbox.commands.ops, [])

    def test_later_missing_copy_prevents_earlier_run(self) -> None:
        context = _MemoryDockerContext(
            "FROM ubuntu\nRUN echo side-effect\nCOPY missing /dst/\n", {}
        )
        sandbox = _MockSandbox()
        with self.assertRaises(DockerfileBuildError) as raised:
            apply_dockerfile(
                sandbox, parse_dockerfile(context), context, auto_start_cmd=False
            )
        self.assertEqual(raised.exception.index, 2)
        self.assertEqual(raised.exception.instruction, "COPY")
        self.assertEqual(context.open_paths, [])
        self.assertEqual(sandbox.files.ops, [])
        self.assertEqual(sandbox.commands.ops, [])

    def test_later_copy_open_failure_prevents_earlier_run(self) -> None:
        context = _MemoryDockerContext(
            "FROM ubuntu\nRUN echo side-effect\nCOPY a /one/\nCOPY b /two/\n",
            {"a": b"a", "b": b"b"},
            fail_path="b",
        )
        sandbox = _MockSandbox()
        with self.assertRaises(DockerfileBuildError) as raised:
            apply_dockerfile(
                sandbox, parse_dockerfile(context), context, auto_start_cmd=False
            )
        self.assertEqual(raised.exception.index, 3)
        self.assertEqual(raised.exception.instruction, "COPY")
        self.assertEqual(context.open_paths, ["a", "b"])
        self.assertEqual(sandbox.files.ops, [])
        self.assertEqual(sandbox.commands.ops, [])

    def test_all_sources_materialize_before_first_sandbox_operation(self) -> None:
        context = _MemoryDockerContext(
            "FROM ubuntu\nRUN echo side-effect\nCOPY a /one/\nCOPY b /two/\n",
            {"a": b"a", "b": b"b"},
        )
        sandbox = _MockSandbox()
        original_open = context.open
        events: list[str] = []
        original_run = sandbox.commands.run
        original_copy = sandbox.files.copy_from_local

        def traced_run(*args, **kwargs):
            events.append("run")
            return original_run(*args, **kwargs)

        def traced_copy(local: str, remote: str) -> None:
            events.append(f"copy:{remote}")
            original_copy(local, remote)

        sandbox.commands.run = traced_run  # type: ignore[method-assign]
        sandbox.files.copy_from_local = traced_copy  # type: ignore[method-assign]

        @contextmanager
        def checked_open(path: str) -> Iterator[BinaryIO]:
            self.assertEqual(sandbox.files.ops, [])
            self.assertEqual(sandbox.commands.ops, [])
            with original_open(path) as stream:
                yield stream

        context.open = checked_open  # type: ignore[method-assign]
        apply_dockerfile(
            sandbox, parse_dockerfile(context), context, auto_start_cmd=False
        )
        self.assertEqual(context.open_paths, ["a", "b"])
        self.assertEqual(
            [operation[2] for operation in sandbox.files.ops if operation[0] == "cp"],
            ["/one/a", "/two/b"],
        )
        self.assertEqual(
            events,
            ["run", "copy:/one/a", "run", "copy:/two/b", "run"],
        )

    def test_manifest_failure_is_wrapped_before_sandbox_operations(self) -> None:
        class BrokenContext(_MemoryDockerContext):
            def walk(self) -> Iterator[DockerContextEntry]:
                raise OSError("walk failed")
                yield DockerContextEntry("unreachable", "file", 0o644)

        context = BrokenContext("FROM ubuntu\nRUN echo never\nCOPY a /dest/\n", {})
        sandbox = _MockSandbox()
        parsed = parse_dockerfile(context)
        with self.assertRaisesRegex(
            DockerfileBuildError, "Docker context manifest.*BrokenContext.*walk failed"
        ):
            apply_dockerfile(sandbox, parsed, context, auto_start_cmd=False)
        self.assertEqual(sandbox.files.ops, [])
        self.assertEqual(sandbox.commands.ops, [])

    def test_relative_destination_resolves_against_workdir(self) -> None:
        sandbox, _ = self._apply_memory(
            "FROM ubuntu\nWORKDIR /work\nCOPY app.py output/\n",
            {"app.py": b"app"},
        )
        copies = [
            operation[2] for operation in sandbox.files.ops if operation[0] == "cp"
        ]
        self.assertEqual(copies, ["/work/output/app.py"])

    def test_workdir_copy_is_prepared_before_workdir_operation(self) -> None:
        context = _MemoryDockerContext(
            "FROM ubuntu\nWORKDIR /work\nCOPY app.py output/\n", {"app.py": b"app"}
        )
        sandbox = _MockSandbox()
        original_open = context.open

        @contextmanager
        def checked_open(path: str) -> Iterator[BinaryIO]:
            self.assertEqual(sandbox.files.ops, [])
            self.assertEqual(sandbox.commands.ops, [])
            with original_open(path) as stream:
                yield stream

        context.open = checked_open  # type: ignore[method-assign]
        apply_dockerfile(
            sandbox, parse_dockerfile(context), context, auto_start_cmd=False
        )
        self.assertEqual(context.open_paths, ["app.py"])
        self.assertEqual(sandbox.files.ops[0], ("mkdir", "/work"))
        self.assertEqual(sandbox.files.ops[-1][2], "/work/output/app.py")

    def test_add_extracts_only_explicit_literal_tar(self) -> None:
        archive = _archive_bytes([_regular_member("app.txt")])
        literal, _ = self._apply_memory(
            "FROM ubuntu\nADD app.tar /out/\n", {"app.tar": archive}
        )
        self.assertTrue(any("tar xf" in op[1] for op in literal.commands.ops))

        wildcard, _ = self._apply_memory(
            "FROM ubuntu\nADD *.tar /out/\n", {"app.tar": b"tar"}
        )
        self.assertEqual(
            [op[2] for op in wildcard.files.ops if op[0] == "cp"], ["/out/app.tar"]
        )
        self.assertFalse(any("tar xf" in op[1] for op in wildcard.commands.ops))

        directory, _ = self._apply_memory(
            "FROM ubuntu\nADD src /out/\n", {"src/app.tar": b"tar"}
        )
        self.assertEqual(
            [op[2] for op in directory.files.ops if op[0] == "cp"], ["/out/app.tar"]
        )
        self.assertFalse(any("tar xf" in op[1] for op in directory.commands.ops))

    def test_directory_copy_chown_skips_existing_destination(self) -> None:
        sandbox, _ = self._apply_memory(
            "FROM ubuntu\nCOPY --chown=app:app src /app\n",
            {"src/a": b"a", "src/sub/b": b"b"},
            existing_paths={"/app"},
        )
        chown = [op[1] for op in sandbox.commands.ops if "chown" in op[1]]
        self.assertEqual(
            chown,
            [
                "chown app:app /app/a",
                "chown app:app /app/sub/b",
                "chown app:app /app/sub",
            ],
        )
        self.assertEqual(sandbox.files.exists_calls, ["/app", "/app/sub"])
        self.assertNotIn("chown app:app /app", chown)
        self.assertFalse(any("-R" in command for command in chown))

    def test_directory_copy_chown_includes_new_destination_marker(self) -> None:
        sandbox, _ = self._apply_memory(
            "FROM ubuntu\nCOPY --chown=app:app src /app\n",
            {"src/a": b"a", "src/sub/b": b"b"},
        )
        chown = [op[1] for op in sandbox.commands.ops if "chown" in op[1]]
        self.assertEqual(
            set(chown),
            {
                "chown app:app /app/a",
                "chown app:app /app/sub/b",
                "chown app:app /app",
                "chown app:app /app/sub",
            },
        )
        self.assertFalse(any("-R" in command for command in chown))

    def test_multiple_copy_chown_uses_exact_file_targets(self) -> None:
        sandbox, _ = self._apply_memory(
            "FROM ubuntu\nCOPY --chown=app:app a b /app/\n",
            {"a": b"a", "b": b"b"},
            existing_paths={"/app"},
        )
        chown = [op[1] for op in sandbox.commands.ops if "chown" in op[1]]
        self.assertEqual(
            chown,
            ["chown app:app /app/a", "chown app:app /app/b"],
        )

    def test_tar_chown_skips_existing_destination_and_uses_exact_targets(self) -> None:
        archive = _archive_bytes([_regular_member("a"), _regular_member("sub/b")])
        sandbox, _ = self._apply_memory(
            "FROM ubuntu\nADD --chown=app:app app.tar /app/\n",
            {"app.tar": archive},
            existing_paths={"/app"},
        )
        chown = [op[1] for op in sandbox.commands.ops if "chown" in op[1]]
        self.assertEqual(
            chown,
            [
                "chown app:app /app/a",
                "chown app:app /app/sub/b",
                "chown app:app /app/sub",
            ],
        )
        self.assertNotIn("chown app:app /app", chown)
        self.assertFalse(any("-R" in command for command in chown))

    def test_invalid_tar_members_fail_before_sandbox_operations(self) -> None:
        symlink = tarfile.TarInfo("link")
        symlink.type = tarfile.SYMTYPE
        symlink.linkname = "target"
        hardlink = tarfile.TarInfo("hard")
        hardlink.type = tarfile.LNKTYPE
        hardlink.linkname = "target"
        fifo = tarfile.TarInfo("pipe")
        fifo.type = tarfile.FIFOTYPE
        device = tarfile.TarInfo("device")
        device.type = tarfile.CHRTYPE
        cases = {
            "symlink": [(symlink, None)],
            "hardlink": [(hardlink, None)],
            "fifo": [(fifo, None)],
            "device": [(device, None)],
            "absolute": [_regular_member("/absolute")],
            "control": [_regular_member("bad\x01name")],
            "backslash": [_regular_member(r"dir\file")],
            "traversal": [_regular_member("../traversal")],
            "duplicate": [_regular_member("same"), _regular_member("same")],
            "file ancestor": [_regular_member("file"), _regular_member("file/child")],
        }
        for name, members in cases.items():
            with self.subTest(name=name):
                sandbox = _MockSandbox()
                context = _MemoryDockerContext(
                    "FROM ubuntu\nADD invalid.tar /app/\n",
                    {"invalid.tar": _archive_bytes(members)},
                )
                with self.assertRaises(DockerfileBuildError) as raised:
                    apply_dockerfile(
                        sandbox,
                        parse_dockerfile(context),
                        context,
                        auto_start_cmd=False,
                    )
                self.assertEqual(raised.exception.index, 1)
                self.assertEqual(raised.exception.instruction, "ADD")
                self.assertEqual(sandbox.files.ops, [])
                self.assertEqual(sandbox.commands.ops, [])

    def test_tar_symlink_precheck_prevents_write_operations(self) -> None:
        sandbox = _MockSandbox(failing_cmd="test ! -L")
        context = _MemoryDockerContext(
            "FROM ubuntu\nADD --chown=app:app app.tar /app/\n",
            {"app.tar": _archive_bytes([_regular_member("app.txt")])},
        )
        with self.assertRaisesRegex(DockerfileBuildError, "symlink"):
            apply_dockerfile(
                sandbox,
                parse_dockerfile(context),
                context,
                auto_start_cmd=False,
            )
        self.assertEqual(sandbox.files.ops, [])
        self.assertEqual(len(sandbox.commands.ops), 1)
        self.assertIn("test ! -L", sandbox.commands.ops[0][1])
        self.assertFalse(any("tar xf" in op[1] for op in sandbox.commands.ops))
        self.assertFalse(any("chown" in op[1] for op in sandbox.commands.ops))


    def test_structured_context_copies_empty_directories_and_modes(self) -> None:
        entries = (
            DockerContextEntry("empty", "directory", 0o711),
            DockerContextEntry("top", "directory", 0o755),
            DockerContextEntry("top/nested", "directory", 0o750),
            DockerContextEntry("plain.txt", "file", 0o640),
            DockerContextEntry("run.sh", "file", 0o755),
        )
        context = _MemoryDockerContext(
            "FROM ubuntu\nCOPY empty/ /srv/empty/\nCOPY . /tree/\n",
            {"plain.txt": b"plain", "run.sh": b"#!/bin/sh\n"},
            entries=entries,
        )
        sandbox = _MockSandbox()
        apply_dockerfile(
            sandbox, parse_dockerfile(context), context, auto_start_cmd=False
        )
        copied = [op[2] for op in sandbox.files.ops if op[0] == "cp"]
        self.assertEqual(copied, ["/tree/plain.txt", "/tree/run.sh"])
        made_dirs = [op[1] for op in sandbox.files.ops if op[0] == "mkdir"]
        self.assertEqual(
            made_dirs,
            [
                "/srv",
                "/srv/empty",
                "/tree",
                "/tree/empty",
                "/tree/top",
                "/tree/top/nested",
            ],
        )
        chmod = [op[1] for op in sandbox.commands.ops if "chmod" in op[1]]
        self.assertEqual(
            chmod,
            [
                "chmod 0711 /tree/empty",
                "chmod 0640 /tree/plain.txt",
                "chmod 0755 /tree/run.sh",
                "chmod 0755 /tree/top",
                "chmod 0750 /tree/top/nested",
            ],
        )
        self.assertEqual(context.open_paths, ["plain.txt", "run.sh"])

    def test_literal_directory_copy_to_root_copies_contents_and_subdirectories(
        self,
    ) -> None:
        entries = (
            DockerContextEntry("src", "directory", 0o700),
            DockerContextEntry("src/a", "file", 0o640),
            DockerContextEntry("src/empty", "directory", 0o711),
        )
        sandbox, context = self._apply_memory(
            "FROM ubuntu\nCOPY src /\n",
            {"src/a": b"a"},
            entries=entries,
        )
        self.assertEqual(
            [operation[2] for operation in sandbox.files.ops if operation[0] == "cp"],
            ["/a"],
        )
        self.assertEqual(
            [
                operation[1]
                for operation in sandbox.files.ops
                if operation[0] == "mkdir"
            ],
            ["/empty"],
        )
        self.assertEqual(
            [
                operation[1]
                for operation in sandbox.commands.ops
                if "chmod" in operation[1]
            ],
            ["chmod 0640 /a", "chmod 0711 /empty"],
        )
        self.assertEqual(context.open_paths, ["src/a"])

    def test_empty_literal_directory_copy_to_root_has_no_sandbox_operations(
        self,
    ) -> None:
        context = _MemoryDockerContext(
            "FROM ubuntu\nCOPY empty /\n",
            {},
            entries=(DockerContextEntry("empty", "directory", 0o700),),
        )
        sandbox = _MockSandbox()
        apply_dockerfile(
            sandbox, parse_dockerfile(context), context, auto_start_cmd=False
        )
        self.assertEqual(sandbox.files.ops, [])
        self.assertEqual(sandbox.commands.ops, [])
        self.assertEqual(context.open_paths, [])

    def test_multiple_literal_directories_share_destination_marker(self) -> None:
        entries = (
            DockerContextEntry("one", "directory", 0o700),
            DockerContextEntry("one/a", "file", 0o640),
            DockerContextEntry("two", "directory", 0o711),
            DockerContextEntry("two/b", "file", 0o600),
        )
        sandbox, _ = self._apply_memory(
            "FROM ubuntu\nCOPY one two /target/\n",
            {"one/a": b"a", "two/b": b"b"},
            entries=entries,
        )
        self.assertEqual(
            [operation[2] for operation in sandbox.files.ops if operation[0] == "cp"],
            ["/target/a", "/target/b"],
        )
        self.assertNotIn(
            "chmod 0700 /target",
            [operation[1] for operation in sandbox.commands.ops],
        )
        self.assertNotIn(
            "chmod 0711 /target",
            [operation[1] for operation in sandbox.commands.ops],
        )

    def test_literal_directory_root_marker_does_not_chmod_destination(self) -> None:
        entries = (
            DockerContextEntry("src", "directory", 0o700),
            DockerContextEntry("src/a", "file", 0o640),
        )
        sandbox, _ = self._apply_memory(
            "FROM ubuntu\nCOPY src /target/\n",
            {"src/a": b"a"},
            existing_paths={"/target"},
            entries=entries,
        )
        chmod = [
            operation[1]
            for operation in sandbox.commands.ops
            if "chmod" in operation[1]
        ]
        self.assertEqual(chmod, ["chmod 0640 /target/a"])
        self.assertNotIn("chmod 0700 /target", chmod)

    def test_runner_commands_always_receive_root_cwd(self) -> None:
        archive = _archive_bytes([_regular_member("inside")])
        context = _MemoryDockerContext(
            "FROM ubuntu\n"
            "USER app\n"
            "RUN echo build\n"
            "COPY input /copy/\n"
            "ADD --chown=app:app app.tar /extract/\n"
            'CMD ["server"]\n',
            {"input": b"input", "app.tar": archive},
        )
        sandbox = _MockSandbox()
        apply_dockerfile(
            sandbox, parse_dockerfile(context), context, auto_start_cmd=True
        )
        commands = [op for op in sandbox.commands.ops if op[0] == "run"]
        self.assertTrue(any("test ! -L" in op[1] for op in commands))
        self.assertTrue(any("tar xf" in op[1] for op in commands))
        self.assertTrue(any("chown app:app" in op[1] for op in commands))
        self.assertTrue(any("chmod 0644 /copy/input" in op[1] for op in commands))
        self.assertTrue(any(op[2] for op in commands))
        self.assertTrue(all(op[4] == "/" for op in commands))
        tar = next(op[1] for op in commands if "tar xf" in op[1])
        self.assertNotIn("runuser", tar)
        self.assertNotIn("su -s", tar)



class TestLocalDockerContext(unittest.TestCase):
    def test_content_string(self):
        ctx = LocalDockerContext("FROM ubuntu\n")
        self.assertEqual(ctx.dockerfile_text(), "FROM ubuntu\n")

    def test_file_path(
        self,
    ):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "Dockerfile")
            with open(p, "w") as _f:
                _f.write("FROM node:20\n")
            ctx = LocalDockerContext(p)
            self.assertEqual(ctx.dockerfile_text(), "FROM node:20\n")
            self.assertEqual(ctx.context_dir, os.path.abspath(d))

    def test_walk_includes_control_files(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "Dockerfile"), "w") as _f:
                _f.write("FROM ubuntu\n")
            os.chmod(os.path.join(d, "Dockerfile"), 0o644)
            with open(os.path.join(d, "app.py"), "w") as _f:
                _f.write("print(1)")
            os.chmod(os.path.join(d, "app.py"), 0o640)
            ctx = LocalDockerContext("FROM ubuntu\n", context_dir=d)
            self.assertEqual(
                list(ctx.walk()),
                [
                    DockerContextEntry("Dockerfile", "file", 0o644),
                    DockerContextEntry("app.py", "file", 0o640),
                ],
            )

    def test_open_path_escape_rejected(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            ctx = LocalDockerContext("FROM ubuntu\n", context_dir=d)
            with self.assertRaises(DockerContextError):
                with ctx.open("../x"):
                    pass

    def test_open_path_inside_context(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "app.py")
            with open(path, "wb") as handle:
                handle.write(b"print(1)")
            ctx = LocalDockerContext("FROM ubuntu\n", context_dir=d)
            with ctx.open("app.py") as handle:
                self.assertEqual(handle.read(), b"print(1)")


class TestStrictDockerfileSemantics(unittest.TestCase):
    def _parse(self, dockerfile, strict=False):
        return parse_dockerfile(LocalDockerContext(dockerfile), strict=strict)

    def test_user_accepts_only_literal_names(self):
        accepted = self._parse("FROM ubuntu\nUSER app\n", strict=True)
        self.assertEqual(accepted.user, "app")
        for value in ("app:staff", "1000", "1000:1001", "app staff", ""):
            with self.subTest(value=value):
                dockerfile = f"FROM ubuntu\nUSER {value}\n"
                parsed = self._parse(dockerfile)
                self.assertEqual(parsed.user, None)
                self.assertFalse(
                    any(
                        isinstance(item, UserInstruction)
                        for item in parsed.instructions
                    )
                )
                result = check_direct_launch(LocalDockerContext(dockerfile))
                self.assertFalse(result.direct_launchable)
                self.assertEqual(result.reasons, ("unsupported_syntax",))
                with self.assertRaises(DockerfileParseError):
                    self._parse(dockerfile, strict=True)

    def test_remote_add_is_never_executable(self):
        dockerfile = "FROM ubuntu\nADD https://example.test/app.tar /opt/\n"
        parsed = self._parse(dockerfile)
        self.assertEqual(parsed.unsupported[0].kind, "ADD")
        self.assertEqual(parsed.unsupported[0].reason, "remote_add")
        self.assertFalse(
            any(isinstance(item, CopyInstruction) for item in parsed.instructions)
        )
        with self.assertRaisesRegex(
            DockerfileParseError, "download into the build context"
        ):
            self._parse(dockerfile, strict=True)
        result = check_direct_launch(LocalDockerContext(dockerfile))
        self.assertFalse(result.direct_launchable)
        self.assertEqual(result.reasons, ("remote_add",))

    def test_apply_rejects_unsupported_before_any_operation(self):
        context = LocalDockerContext("FROM ubuntu\nADD http://example.test/a /opt/\n")
        parsed = parse_dockerfile(context)
        sandbox = _MockSandbox()
        with self.assertRaisesRegex(DockerfileBuildError, "unsupported instruction"):
            apply_dockerfile(sandbox, parsed, context)
        self.assertEqual(sandbox.files.ops, [])
        self.assertEqual(sandbox.commands.ops, [])

    def test_supported_shell_run_absolute_workdir_and_chown(self):
        parsed = self._parse(
            "FROM ubuntu\nRUN echo hi\nWORKDIR /app\nCOPY --chown=app:app x /app/\n",
            strict=True,
        )
        self.assertFalse(parsed.unsupported)
        self.assertEqual(parsed.workdir, "/app")
        copy = next(
            item for item in parsed.instructions if isinstance(item, CopyInstruction)
        )
        self.assertEqual(copy.chown, "app:app")

    def test_unsupported_syntax_is_not_executable_and_not_launchable(self):
        cases = {
            "json COPY": 'COPY ["a b", "/dest/"]',
            "json ADD": 'ADD ["a b", "/dest/"]',
            "exec RUN": 'RUN ["printf", "%s", "x"]',
            "relative WORKDIR": "WORKDIR app",
            "ARG": "ARG VERSION=1",
            "chmod": "COPY --chmod=755 a /dest/",
            "link": "COPY --link a /dest/",
            "unknown flag": "COPY --parents a /dest/",
            "FROM platform": "FROM --platform=linux/amd64 ubuntu",
        }
        for name, instruction in cases.items():
            with self.subTest(name=name):
                dockerfile = (
                    instruction
                    if instruction.startswith("FROM")
                    else f"FROM ubuntu\n{instruction}\n"
                )
                parsed = self._parse(dockerfile)
                self.assertTrue(parsed.unsupported)
                self.assertFalse(
                    any(
                        isinstance(item, (CopyInstruction, RunInstruction))
                        for item in parsed.instructions
                    )
                )
                self.assertFalse(
                    check_direct_launch(
                        LocalDockerContext(dockerfile)
                    ).direct_launchable
                )
                with self.assertRaises(DockerfileParseError):
                    self._parse(dockerfile, strict=True)

    def test_ignored_and_unknown_instructions_fail_closed(self):
        for kind, value in (
            ("VOLUME", "/data"),
            ("LABEL", "k=v"),
            ("HEALTHCHECK", "CMD true"),
            ("SHELL", '["/bin/bash", "-c"]'),
            ("STOPSIGNAL", "SIGTERM"),
            ("ONBUILD", "RUN echo x"),
            ("MAINTAINER", "someone"),
            ("FUTURE", "value"),
        ):
            with self.subTest(kind=kind):
                dockerfile = f"FROM ubuntu\n{kind} {value}\n"
                self.assertEqual(self._parse(dockerfile).unsupported[0].kind, kind)
                result = check_direct_launch(LocalDockerContext(dockerfile))
                self.assertFalse(result.direct_launchable)
                self.assertEqual(result.reasons, ("unsupported_instruction",))
                with self.assertRaises(DockerfileParseError):
                    self._parse(dockerfile, strict=True)

    def test_malformed_quotes_do_not_fall_back_to_whitespace_splitting(self):
        dockerfile = 'FROM ubuntu\nCOPY "unterminated /dest/\n'
        parsed = self._parse(dockerfile)
        self.assertEqual(parsed.unsupported[0].kind, "COPY")
        self.assertFalse(
            any(isinstance(item, CopyInstruction) for item in parsed.instructions)
        )
        self.assertFalse(
            check_direct_launch(LocalDockerContext(dockerfile)).direct_launchable
        )
        with self.assertRaisesRegex(DockerfileParseError, "malformed quoting"):
            self._parse(dockerfile, strict=True)

    def test_build_time_variable_expansion_fails_closed(self):
        cases = (
            "FROM ${IMAGE}",
            "FROM ubuntu\nWORKDIR /app/$NAME",
            "FROM ubuntu\nCOPY $SRC /app/",
            "FROM ubuntu\nADD ${SRC} /app/",
            "FROM ubuntu\nUSER $USER",
            "FROM ubuntu\nENV K=$VALUE",
        )
        for dockerfile in cases:
            with self.subTest(dockerfile=dockerfile):
                self.assertFalse(
                    check_direct_launch(
                        LocalDockerContext(dockerfile)
                    ).direct_launchable
                )
                with self.assertRaises(DockerfileParseError):
                    self._parse(dockerfile, strict=True)

    def test_copy_from_uses_multi_stage_reason(self):
        dockerfile = "FROM ubuntu\nCOPY --from=build x /y\n"
        result = check_direct_launch(LocalDockerContext(dockerfile))
        self.assertFalse(result.direct_launchable)
        self.assertEqual(result.reasons, ("multi_stage",))
        with self.assertRaises(DockerfileParseError):
            self._parse(dockerfile, strict=True)

    def test_cmd_and_entrypoint_resolve_to_executable_argv(self):
        cases = (
            ('CMD ["a", "b"]', ("a", "b")),
            ("CMD echo hello", ("/bin/sh", "-c", "echo hello")),
            ('ENTRYPOINT ["python3"]', ("python3",)),
            ('ENTRYPOINT ["python3"]\nCMD ["app.py"]', ("python3", "app.py")),
            (
                'ENTRYPOINT ["python3"]\nCMD echo hello',
                ("python3", "/bin/sh", "-c", "echo hello"),
            ),
            (
                'ENTRYPOINT echo entry\nCMD ["ignored"]',
                ("/bin/sh", "-c", "echo entry"),
            ),
        )
        for body, expected in cases:
            with self.subTest(body=body):
                parsed = self._parse(f"FROM ubuntu\n{body}\n", strict=True)
                self.assertEqual(parsed.start_cmd, expected)
        shell_entrypoint = self._parse(
            'FROM ubuntu\nENTRYPOINT echo entry\nCMD ["ignored"]\n', strict=True
        )
        self.assertEqual(shell_entrypoint.entrypoint, ("/bin/sh", "-c", "echo entry"))

    def test_json_array_detection_requires_a_complete_json_array(self):
        self.assertEqual(_json_array('["echo", "ok"]'), ["echo", "ok"])
        self.assertIsNone(_json_array("[ -f /tmp/x ] && echo yes"))
        self.assertIsNone(_json_array("[file /dest/"))

    def test_bracket_prefixed_shell_forms_are_supported(self):
        run = self._parse("FROM ubuntu\nRUN [ -f /tmp/x ] && echo yes\n", strict=True)
        instruction = next(
            item for item in run.instructions if isinstance(item, RunInstruction)
        )
        self.assertEqual(instruction.command, "[ -f /tmp/x ] && echo yes")

        cmd = self._parse("FROM ubuntu\nCMD [ -f /tmp/x ] && echo yes\n", strict=True)
        self.assertEqual(
            cmd.start_cmd,
            ("/bin/sh", "-c", "[ -f /tmp/x ] && echo yes"),
        )

        copy = self._parse("FROM ubuntu\nCOPY [file /dest/\n", strict=True)
        copy_instruction = next(
            item for item in copy.instructions if isinstance(item, CopyInstruction)
        )
        self.assertEqual(copy_instruction.srcs, ("[file",))
        self.assertEqual(copy_instruction.dest, "/dest/")

    def test_legacy_env_uses_shlex_processed_value(self):
        parsed = self._parse('FROM ubuntu\nENV FOO "bar baz"\n', strict=True)
        self.assertEqual(parsed.envs, {"FOO": "bar baz"})

        empty_assignment = self._parse("FROM ubuntu\nENV FOO=\n", strict=True)
        self.assertEqual(empty_assignment.envs, {"FOO": ""})

    def test_env_without_value_is_unsupported(self):
        dockerfile = "FROM ubuntu\nENV FOO\n"
        result = check_direct_launch(LocalDockerContext(dockerfile))
        self.assertFalse(result.direct_launchable)
        self.assertEqual(result.reasons, ("unsupported_syntax",))
        with self.assertRaisesRegex(DockerfileParseError, "requires a key and value"):
            self._parse(dockerfile, strict=True)

    def test_multi_source_copy_destination_must_end_in_slash(self):
        rejected = "FROM ubuntu\nCOPY a b /dest\n"
        result = check_direct_launch(LocalDockerContext(rejected))
        self.assertFalse(result.direct_launchable)
        self.assertEqual(result.reasons, ("unsupported_syntax",))
        with self.assertRaisesRegex(
            DockerfileParseError, "destination must be a directory"
        ):
            self._parse(rejected, strict=True)

        parsed = self._parse("FROM ubuntu\nCOPY a b /dest/\n", strict=True)
        instruction = next(
            item for item in parsed.instructions if isinstance(item, CopyInstruction)
        )
        self.assertEqual(instruction.srcs, ("a", "b"))
        self.assertEqual(instruction.dest, "/dest/")

    def test_empty_cmd_and_entrypoint_are_unsupported(self):
        cases = (
            "CMD []",
            "ENTRYPOINT []",
            'CMD [""]',
            'ENTRYPOINT [""]',
            "CMD",
            "ENTRYPOINT",
        )
        for instruction in cases:
            with self.subTest(instruction=instruction):
                dockerfile = f"FROM ubuntu\n{instruction}\n"
                result = check_direct_launch(LocalDockerContext(dockerfile))
                self.assertFalse(result.direct_launchable)
                self.assertEqual(result.reasons, ("unsupported_syntax",))
                with self.assertRaises(DockerfileParseError):
                    self._parse(dockerfile, strict=True)

    def test_launcher_uses_shlex_join_for_single_exec_argument_with_spaces(self):
        context = LocalDockerContext('FROM ubuntu\nCMD ["hello world"]\n')
        sandbox = _MockSandbox()
        result = apply_dockerfile(
            sandbox,
            parse_dockerfile(context, strict=True),
            context,
            auto_start_cmd=True,
        )
        self.assertEqual(result.start_cmd, ("hello world",))
        background = next(
            operation for operation in sandbox.commands.ops if operation[2]
        )
        self.assertEqual(background[1], "'hello world'")


if __name__ == "__main__":
    unittest.main()
