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

"""Launch sandboxes from the strict Dockerfile direct-launch subset.

Each section builds a separate local context and creates a fresh sandbox. The
``FROM`` image provides only the root filesystem; the Dockerfile explicitly
sets runtime state. RUN/COPY/ADD execute for every launch, without a snapshot
or build cache.

Sections:
    1. Core path, ignore filtering, wildcard COPY, modes, and empty directories
    2. Root cwd plus ENTRYPOINT + CMD exec-form combination
    3. Exec-form ENTRYPOINT without CMD
    4. Shell-form CMD
    5. Shell-form ENTRYPOINT ignoring CMD
    6. Disabled automatic startup dispatch
    7. COPY --chown ownership
    8. Builder/root ADD after USER
    9. Fail-closed pre-check without a sandbox
"""

import tarfile
import tempfile
from pathlib import Path

from akernel_sdk import DockerfileLaunch, LocalDockerContext, Sandbox, check_direct_launch


def _precheck(context: LocalDockerContext) -> None:
    """Print diagnostics and assert direct launch is available."""
    result = check_direct_launch(context)
    for warning in result.warnings:
        print(f"  precheck warning: {warning}")
    assert result.direct_launchable, (result.reasons, result.warnings)


def section_core_path() -> None:
    """Copy a companion-filtered context and wildcard directory."""
    print("\n=== Section 1: companion-filtered core path ===")
    with tempfile.TemporaryDirectory() as directory:
        context_dir = Path(directory)
        build = context_dir / "build"
        build.mkdir()
        (context_dir / ".dockerignore").write_text("greeting.txt\n", encoding="utf-8")
        (context_dir / "secret.txt").write_text("do not upload\n", encoding="utf-8")
        (context_dir / "greeting.txt").write_text("hello\n", encoding="utf-8")
        docs = context_dir / "docs"
        docs.mkdir()
        (docs / "README.md").write_text("visible\n", encoding="utf-8")
        (docs / "private.txt").write_text("hidden\n", encoding="utf-8")
        (context_dir / "app.py").write_text(
            "import os\n"
            "open('/tmp/app.started', 'w').write(\n"
            "    f\"whoami={os.environ['WHOAMI']} cwd={os.getcwd()}\"\n"
            ")\n",
            encoding="utf-8",
        )
        executable = context_dir / "entrypoint.sh"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        empty = context_dir / "empty"
        nested_empty = context_dir / "tree" / "nested-empty"
        wildcard_file = context_dir / "wild" / "dir1" / "dir2" / "foo"
        empty.mkdir()
        nested_empty.mkdir(parents=True)
        wildcard_file.parent.mkdir(parents=True)
        wildcard_file.write_text("wildcard\n", encoding="utf-8")
        empty.chmod(0o711)
        nested_empty.chmod(0o750)
        dockerfile = build / "custom.Dockerfile"
        dockerfile.write_text(
            """FROM ubuntu:22.04
RUN apt-get update && apt-get install -y --no-install-recommends python3
RUN useradd -m app
ENV WHOAMI=app
WORKDIR /srv
USER app
COPY greeting.txt /srv/control/greeting.txt
COPY build/custom.Dockerfile /srv/control/
COPY .dockerignore /srv/control/
COPY build/*.dockerignore /srv/control/
COPY empty/ /srv/core/literal-empty/
COPY . /srv/core/
COPY wild/* /srv/wild/
COPY docs /srv/reincluded-literal/
COPY doc* /srv/reincluded-wildcard/
CMD ["python3", "/srv/core/app.py"]
""",
            encoding="utf-8",
        )
        (build / "custom.Dockerfile.dockerignore").write_text(
            "secret.txt\ndocs\n!docs/README.md\n", encoding="utf-8"
        )
        context = LocalDockerContext(dockerfile, context_dir=context_dir)
        _precheck(context)

        with Sandbox(dockerfile=DockerfileLaunch(context, run_timeout=300)) as sandbox:
            startup = sandbox.startup_command
            assert startup is not None
            result = startup.wait(timeout=60)
            assert result.exit_code == 0, result.stderr
            marker = sandbox.commands.run("cat /tmp/app.started")
            assert marker.exit_code == 0, marker.stderr
            assert marker.stdout.strip() == "whoami=app cwd=/srv", marker.stdout
            controls = sandbox.commands.run(
                "test -f /srv/control/greeting.txt "
                "&& test -f /srv/control/custom.Dockerfile "
                "&& test -f /srv/control/.dockerignore "
                "&& test -f /srv/control/custom.Dockerfile.dockerignore "
                "&& test -f /srv/core/greeting.txt "
                "&& test -f /srv/core/build/custom.Dockerfile "
                "&& test -f /srv/core/.dockerignore "
                "&& test -f /srv/core/build/custom.Dockerfile.dockerignore "
                "&& test ! -e /srv/core/secret.txt"
            )
            assert controls.exit_code == 0, controls.stderr
            modes = sandbox.commands.run(
                "stat -c '%a' /srv/core/entrypoint.sh /srv/core/empty "
                "/srv/core/tree/nested-empty /srv/core/literal-empty"
            )
            assert modes.exit_code == 0, modes.stderr
            assert modes.stdout.splitlines() == ["755", "711", "750", "755"], modes.stdout
            wildcard = sandbox.commands.run(
                "test -f /srv/wild/dir2/foo && test ! -e /srv/wild/dir1"
            )
            assert wildcard.exit_code == 0, wildcard.stderr
            reincluded = sandbox.commands.run(
                "test -f /srv/core/docs/README.md "
                "&& test ! -e /srv/core/docs/private.txt "
                "&& test -f /srv/reincluded-literal/README.md "
                "&& test ! -e /srv/reincluded-literal/private.txt "
                "&& test -f /srv/reincluded-wildcard/README.md "
                "&& test ! -e /srv/reincluded-wildcard/private.txt"
            )
            assert reincluded.exit_code == 0, reincluded.stderr
            print(
                f"  sandbox: {sandbox.id}; marker: {marker.stdout.strip()}; "
                f"modes: {modes.stdout.splitlines()}"
            )


def section_entrypoint_cmd_merge() -> None:
    """Wait for an ENTRYPOINT + CMD command before reading its marker."""
    print("\n=== Section 2: ENTRYPOINT + CMD ===")
    with tempfile.TemporaryDirectory() as directory:
        context_dir = Path(directory)
        dockerfile = """\
FROM ubuntu:22.04
RUN test "$(pwd)" = /
ENTRYPOINT ["/bin/sh", "-c", "printf %s \\\"$1\\\" > /tmp/ep.out; pwd > /tmp/cwd.out"]
CMD ["ignored-argv-zero", "entrypoint+cmd merged"]
"""
        context = LocalDockerContext(dockerfile, context_dir=context_dir)
        _precheck(context)

        with Sandbox(dockerfile=DockerfileLaunch(context)) as sandbox:
            startup = sandbox.startup_command
            assert startup is not None
            result = startup.wait(timeout=60)
            assert result.exit_code == 0, result.stderr
            output = sandbox.commands.run("cat /tmp/ep.out")
            assert output.exit_code == 0, output.stderr
            assert output.stdout == "entrypoint+cmd merged", output.stdout
            cwd = sandbox.commands.run("cat /tmp/cwd.out")
            assert cwd.exit_code == 0, cwd.stderr
            assert cwd.stdout.strip() == "/", cwd.stdout
            print(
                f"  sandbox: {sandbox.id}; output: {output.stdout}; "
                f"startup cwd: {cwd.stdout.strip()}"
            )


def section_entrypoint_only() -> None:
    """Dispatch an exec-form ENTRYPOINT when CMD is absent."""
    print("\n=== Section 3: ENTRYPOINT only ===")
    with tempfile.TemporaryDirectory() as directory:
        context = LocalDockerContext(
            "FROM ubuntu:22.04\n"
            'ENTRYPOINT ["/bin/sh", "-c", '
            '"printf entrypoint-only > /tmp/entrypoint-only.out"]\n',
            context_dir=directory,
        )
        _precheck(context)

        with Sandbox(dockerfile=DockerfileLaunch(context)) as sandbox:
            startup = sandbox.startup_command
            assert startup is not None
            result = startup.wait(timeout=60)
            assert result.exit_code == 0, result.stderr
            marker = sandbox.commands.run("cat /tmp/entrypoint-only.out")
            assert marker.exit_code == 0, marker.stderr
            assert marker.stdout == "entrypoint-only", marker.stdout
            print(f"  sandbox: {sandbox.id}; output: {marker.stdout}")


def section_shell_cmd() -> None:
    """Dispatch a shell-form CMD with the declared WORKDIR."""
    print("\n=== Section 4: shell-form CMD ===")
    with tempfile.TemporaryDirectory() as directory:
        context = LocalDockerContext(
            "FROM ubuntu:22.04\nWORKDIR /tmp\nCMD pwd > /tmp/shell-cmd.out\n",
            context_dir=directory,
        )
        _precheck(context)

        with Sandbox(dockerfile=DockerfileLaunch(context)) as sandbox:
            startup = sandbox.startup_command
            assert startup is not None
            result = startup.wait(timeout=60)
            assert result.exit_code == 0, result.stderr
            marker = sandbox.commands.run("cat /tmp/shell-cmd.out")
            assert marker.exit_code == 0, marker.stderr
            assert marker.stdout.strip() == "/tmp", marker.stdout
            print(f"  sandbox: {sandbox.id}; cwd: {marker.stdout.strip()}")


def section_shell_entrypoint_ignores_cmd() -> None:
    """Ensure shell-form ENTRYPOINT replaces rather than appends CMD."""
    print("\n=== Section 5: shell ENTRYPOINT ignores CMD ===")
    with tempfile.TemporaryDirectory() as directory:
        context = LocalDockerContext(
            "FROM ubuntu:22.04\n"
            "ENTRYPOINT printf shell-entrypoint > /tmp/shell-entrypoint.out\n"
            'CMD ["/bin/sh", "-c", '
            '"printf unexpected > /tmp/cmd-should-not-run.out"]\n',
            context_dir=directory,
        )
        _precheck(context)

        with Sandbox(dockerfile=DockerfileLaunch(context)) as sandbox:
            startup = sandbox.startup_command
            assert startup is not None
            result = startup.wait(timeout=60)
            assert result.exit_code == 0, result.stderr
            marker = sandbox.commands.run(
                "cat /tmp/shell-entrypoint.out && test ! -e /tmp/cmd-should-not-run.out"
            )
            assert marker.exit_code == 0, marker.stderr
            assert marker.stdout == "shell-entrypoint", marker.stdout
            print(f"  sandbox: {sandbox.id}; output: {marker.stdout}")


def section_auto_start_disabled() -> None:
    """Keep CMD undispatched when auto_start_cmd is disabled."""
    print("\n=== Section 6: auto startup disabled ===")
    with tempfile.TemporaryDirectory() as directory:
        context = LocalDockerContext(
            "FROM ubuntu:22.04\n"
            'CMD ["/bin/sh", "-c", '
            '"printf unexpected > /tmp/disabled-start.out"]\n',
            context_dir=directory,
        )
        _precheck(context)

        with Sandbox(dockerfile=DockerfileLaunch(context, auto_start_cmd=False)) as sandbox:
            assert sandbox.startup_command is None
            absent = sandbox.commands.run("test ! -e /tmp/disabled-start.out")
            assert absent.exit_code == 0, absent.stderr
            print(f"  sandbox: {sandbox.id}; startup dispatch: disabled")


def section_copy_chown() -> None:
    """Verify COPY --chown without dispatching a startup command."""
    print("\n=== Section 7: COPY --chown ===")
    with tempfile.TemporaryDirectory() as directory:
        context_dir = Path(directory)
        (context_dir / "payload.txt").write_text("owned\n", encoding="utf-8")
        dockerfile = """\
FROM ubuntu:22.04
RUN useradd -m myuser
COPY --chown=myuser:myuser payload.txt /data/payload.txt
"""
        context = LocalDockerContext(dockerfile, context_dir=context_dir)
        _precheck(context)

        with Sandbox(dockerfile=DockerfileLaunch(context)) as sandbox:
            assert sandbox.startup_command is None
            owner = sandbox.commands.run("stat -c '%U:%G' /data/payload.txt")
            assert owner.exit_code == 0, owner.stderr
            assert owner.stdout.strip() == "myuser:myuser", owner.stdout
            print(f"  sandbox: {sandbox.id}; owner: {owner.stdout.strip()}")


def section_add_tar() -> None:
    """Verify literal local ADD tar extraction without a startup command."""
    print("\n=== Section 8: ADD local tar ===")
    with tempfile.TemporaryDirectory() as directory:
        context_dir = Path(directory)
        payload = context_dir / "payload"
        nested = payload / "nested"
        nested.mkdir(parents=True)
        (payload / "top.txt").write_text("top\n", encoding="utf-8")
        (nested / "child.txt").write_text("child\n", encoding="utf-8")
        archive = context_dir / "app.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(payload / "top.txt", arcname="top.txt")
            tar.add(nested, arcname="nested")

        context = LocalDockerContext(
            "FROM ubuntu:22.04\n"
            "RUN useradd -m app\n"
            "USER app\n"
            "ADD app.tar.gz /opt/app/\n",
            context_dir=context_dir,
        )
        _precheck(context)

        with Sandbox(dockerfile=DockerfileLaunch(context)) as sandbox:
            assert sandbox.startup_command is None
            listing = sandbox.commands.run("find /opt/app -type f -printf '%P\n'")
            assert listing.exit_code == 0, listing.stderr
            assert set(listing.stdout.splitlines()) == {"nested/child.txt", "top.txt"}
            print(f"  sandbox: {sandbox.id}; files: {listing.stdout.strip()}")


def section_fail_closed_precheck() -> None:
    """Reject remote ADD and unsupported USER forms before sandbox creation."""
    print("\n=== Section 9: fail-closed precheck ===")
    cases = (
        (
            "FROM ubuntu:22.04\nADD https://example.test/app.tar /opt/app/\n",
            "remote_add",
        ),
        ("FROM ubuntu:22.04\nUSER app:staff\n", "unsupported_syntax"),
        ("FROM ubuntu:22.04\nUSER 1000:1001\n", "unsupported_syntax"),
    )
    with tempfile.TemporaryDirectory() as directory:
        for dockerfile, reason in cases:
            context = LocalDockerContext(dockerfile, context_dir=directory)
            result = check_direct_launch(context)
            assert not result.direct_launchable
            assert reason in result.reasons, result
            print(f"  rejected reasons: {', '.join(result.reasons)}")


def main() -> None:
    section_core_path()
    section_entrypoint_cmd_merge()
    section_entrypoint_only()
    section_shell_cmd()
    section_shell_entrypoint_ignores_cmd()
    section_auto_start_disabled()
    section_copy_chown()
    section_add_tar()
    section_fail_closed_precheck()
    print("\nAll Dockerfile direct-launch sections passed.")


if __name__ == "__main__":
    main()
