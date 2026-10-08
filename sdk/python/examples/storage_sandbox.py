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

"""Verify gVisor writable storage limits with equal and smaller scheduling quotas."""

from akernel_sdk import Sandbox

HARD_LIMIT_MB = 256
SCHEDULING_QUOTA_MB = 64


def verify_hard_limit(sandbox: Sandbox, *, successful_write_mb: int) -> None:
    capacity = sandbox.commands.run("df -m /")
    assert capacity.exit_code == 0, capacity.stderr
    print(capacity.stdout)

    # Write real data and flush it so this checks the writable layer's quota.
    successful_write = sandbox.commands.run(
        "dd if=/dev/zero of=/root/quota-ok bs=1M "
        f"count={successful_write_mb} conv=fsync"
    )
    assert successful_write.exit_code == 0, successful_write.stderr

    oversized_write = sandbox.commands.run(
        "dd if=/dev/zero of=/root/quota-over bs=1M "
        f"count={HARD_LIMIT_MB + 64} conv=fsync"
    )
    assert oversized_write.exit_code != 0, "storage hard limit was not enforced"
    assert "No space left on device" in oversized_write.stderr, oversized_write.stderr


def main() -> None:
    with Sandbox(storage_mb=HARD_LIMIT_MB, cpu=1000, memory=2048) as sandbox:
        verify_hard_limit(sandbox, successful_write_mb=32)
        print(f"Omitted hard limit follows the {HARD_LIMIT_MB} MiB scheduling quota")

    with Sandbox(
        storage_mb=SCHEDULING_QUOTA_MB,
        storage_limit_mb=HARD_LIMIT_MB,
        cpu=1000,
        memory=2048,
    ) as sandbox:
        verify_hard_limit(sandbox, successful_write_mb=2 * SCHEDULING_QUOTA_MB)
        print(
            f"Wrote beyond the {SCHEDULING_QUOTA_MB} MiB scheduling quota; "
            f"the {HARD_LIMIT_MB} MiB hard limit was enforced"
        )


if __name__ == "__main__":
    main()
