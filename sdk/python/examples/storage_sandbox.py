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

"""Verify an experimental gVisor writable root filesystem quota."""

from akernel_sdk import Sandbox

STORAGE_MB = 256


def main() -> None:
    with Sandbox(storage_mb=STORAGE_MB, cpu=1000, memory=2048) as sandbox:
        small_write = sandbox.commands.run(
            "dd if=/dev/zero of=/root/quota-ok bs=1M count=32 conv=fsync"
        )
        assert small_write.exit_code == 0, small_write.stderr

        oversized_write = sandbox.commands.run(
            "dd if=/dev/zero of=/root/quota-over bs=1M count=320 conv=fsync"
        )
        assert oversized_write.exit_code != 0, "storage quota was not enforced"
        assert "No space left on device" in oversized_write.stderr, (
            oversized_write.stderr
        )
        print(f"Writable rootfs quota enforced at {STORAGE_MB} MiB")


if __name__ == "__main__":
    main()
