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

"""Trigger an internal local checkpoint and roll back the same sandbox."""

import os

from akernel_sdk import Sandbox


_INSTALL_CURL_COMMAND = (
    "apt-get update && "
    "DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends curl"
)

_CHECKPOINT_COMMAND = (
    "curl --fail-with-body --silent --show-error "
    "--unix-socket /run/akernel/rrt.sock "
    "--request POST http://localhost/checkpoint"
)


def main() -> None:
    runtime = os.environ.get("AKERNEL_TEST_RUNTIME", "runsc")
    with Sandbox(
        runtime=runtime,
        cpu=2000,
        memory=4096,
        storage_mb=1024,
        failover=True,
    ) as sandbox:
        install_curl = sandbox.commands.run(_INSTALL_CURL_COMMAND, timeout=300)
        assert install_curl.exit_code == 0, install_curl.stderr

        sandbox.commands.run("printf before > /tmp/reload-state")
        checkpoint = sandbox.commands.run(_CHECKPOINT_COMMAND, timeout=300)
        assert checkpoint.exit_code == 0, checkpoint.stderr

        sandbox.commands.run("printf after > /tmp/reload-state")
        assert sandbox.reload()
        state = sandbox.commands.run("cat /tmp/reload-state")
        assert state.stdout == "before"
        print("reloaded:", sandbox.id, "state:", state.stdout)


if __name__ == "__main__":
    main()
