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
    1. Core path — filtered COPY ., wildcard directory COPY, modes, empty dirs
    2. Root cwd plus ENTRYPOINT + CMD exec-form combination
    3. COPY --chown ownership
    4. Builder/root ADD after USER
    5. Fail-closed pre-check without a sandbox
"""

import tarfile
import tempfile
from pathlib import Path

from akernel_sdk import LocalDockerContext, Sandbox, check_direct_launch


def _precheck(context: LocalDockerContext) -> None:
    """Print diagnostics and assert direct launch is available."""
    result = check_direct_launch(context)
    for warning in result.warnings:
        print(f"  precheck warning: {warning}")
    assert result.direct_launchable, (result.reasons, result.warnings)


def section_core_path() -> None:
    """Copy a filtered context and wildcard directory, then run a finite CMD."""
    print("\n=== Section 1: filtered core path ===")
    with tempfile.TemporaryDirectory() as directory:
        context_dir = Path(directory)
        (context_dir / ".dockerignore").write_text("secret.txt\n", encoding="utf-8")
        (context_dir / "secret.txt").write_text("do not upload\n", encoding="utf-8")
        (context_dir / "app.py").write_text(
            "import os\n"
            "open('/tmp/app.started', 'w').write(\n"
            "    f\"whoami={os.environ['WHOAMI']} cwd={os.getcwd()}\"\n"
            ")\n",
            encoding="utf-8",
        )
        (context_dir / "greeting.txt").write_text("hello\n", encoding="utf-8")
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
        dockerfile = """\
FROM ubuntu:22.04
RUN apt-get update && apt-get install -y --no-install-recommends python3
RUN useradd -m app
ENV WHOAMI=app
WORKDIR /srv
USER app
COPY empty/ /srv/core/literal-empty/
COPY . /srv/core/
COPY wild/* /srv/wild/
CMD ["python3", "/srv/core/app.py"]
"""
        context = LocalDockerContext(dockerfile, context_dir=context_dir)
        _precheck(context)

        with Sandbox(context=context, build_run_timeout=300) as sandbox:
            startup = sandbox.startup_command
            assert startup is not None
            result = startup.wait(timeout=60)
            assert result.exit_code == 0, result.stderr

            marker = sandbox.commands.run("cat /tmp/app.started")
            assert marker.exit_code == 0, marker.stderr
            assert marker.stdout.strip() == "whoami=app cwd=/srv", marker.stdout
            absent = sandbox.commands.run("test ! -e /srv/core/secret.txt")
            assert absent.exit_code == 0, absent.stderr
            modes = sandbox.commands.run(
                "stat -c '%a' /srv/core/entrypoint.sh /srv/core/empty "
                "/srv/core/tree/nested-empty /srv/core/literal-empty"
            )
            assert modes.exit_code == 0, modes.stderr
            assert modes.stdout.splitlines() == [
                "755",
                "711",
                "750",
                "755",
            ], modes.stdout
            wildcard = sandbox.commands.run(
                "test -f /srv/wild/dir2/foo && test ! -e /srv/wild/dir1"
            )
            assert wildcard.exit_code == 0, wildcard.stderr
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

        with Sandbox(context=context) as sandbox:
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


def section_copy_chown() -> None:
    """Verify COPY --chown without dispatching a startup command."""
    print("\n=== Section 3: COPY --chown ===")
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

        with Sandbox(context=context) as sandbox:
            assert sandbox.startup_command is None
            owner = sandbox.commands.run("stat -c '%U:%G' /data/payload.txt")
            assert owner.exit_code == 0, owner.stderr
            assert owner.stdout.strip() == "myuser:myuser", owner.stdout
            print(f"  sandbox: {sandbox.id}; owner: {owner.stdout.strip()}")


def section_add_tar() -> None:
    """Verify literal local ADD tar extraction without a startup command."""
    print("\n=== Section 4: ADD local tar ===")
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

        with Sandbox(context=context) as sandbox:
            assert sandbox.startup_command is None
            listing = sandbox.commands.run("find /opt/app -type f -printf '%P\n'")
            assert listing.exit_code == 0, listing.stderr
            assert set(listing.stdout.splitlines()) == {"nested/child.txt", "top.txt"}
            print(f"  sandbox: {sandbox.id}; files: {listing.stdout.strip()}")


def section_fail_closed_precheck() -> None:
    """Reject remote ADD and unsupported USER forms before sandbox creation."""
    print("\n=== Section 5: fail-closed precheck ===")
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
    section_copy_chown()
    section_add_tar()
    section_fail_closed_precheck()
    print("\nAll Dockerfile direct-launch sections passed.")


if __name__ == "__main__":
    main()
