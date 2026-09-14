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

"""Create a sandbox and exercise its core command and filesystem APIs."""

import tempfile
from pathlib import Path

from akernel_sdk import Sandbox


def main() -> None:
    # Context exit explicitly cleans up; GC does not delete the sandbox.
    with Sandbox(cpu=1000, memory=2048) as sandbox:
        print(f"Sandbox created: {sandbox.id}")

        result = sandbox.commands.run(
            'printf "Hello, $USER_NAME!"',
            envs={"USER_NAME": "AKernel"},
        )
        assert result.exit_code == 0, result.stderr
        assert result.stdout == "Hello, AKernel!", result.stdout
        print(f"Command output: {result.stdout}")

        sandbox.files.write("/tmp/hello.txt", "hello from the SDK")
        assert sandbox.files.read("/tmp/hello.txt") == "hello from the SDK"
        entry = sandbox.files.get_info("/tmp/hello.txt")
        print(f"Remote file: {entry.path} ({entry.size} bytes)")

        with tempfile.TemporaryDirectory() as directory:
            local_source = Path(directory) / "upload.txt"
            local_target = Path(directory) / "download.txt"
            local_source.write_text("copy round trip", encoding="utf-8")
            sandbox.files.copy_from_local(str(local_source), "/tmp/upload.txt")
            sandbox.files.copy_to_local("/tmp/upload.txt", str(local_target))
            assert local_target.read_text(encoding="utf-8") == "copy round trip"
            print("File copy round trip: OK")

        handle = sandbox.commands.run("sleep 30", background=True)
        process = next(
            item for item in sandbox.commands.list() if item.pid == handle.pid
        )
        print(
            f"Background command: pid={process.pid} "
            f"command={process.command!r} running={process.running}"
        )
        handle.kill()

        info = sandbox.get_info()
        assert info.id == sandbox.id
        assert sandbox.is_running()
        print(f"Sandbox state: {info.state}")

    print("Sandbox terminated.")


if __name__ == "__main__":
    main()
