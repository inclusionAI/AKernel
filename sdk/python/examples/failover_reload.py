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


_CHECKPOINT_COMMAND = r"""python3 - <<'PY'
import socket

request = (
    b"POST /checkpoint HTTP/1.1\r\n"
    b"Host: localhost\r\n"
    b"Content-Length: 0\r\n"
    b"Connection: close\r\n\r\n"
)
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
    client.settimeout(300)
    client.connect("/run/akernel/rrt.sock")
    client.sendall(request)
    response = bytearray()
    while True:
        chunk = client.recv(4096)
        if not chunk:
            break
        response.extend(chunk)

status = bytes(response).split(b"\r\n", 1)[0]
if b" 200 " not in status:
    raise RuntimeError(bytes(response).decode("utf-8", "replace"))
print(bytes(response).rsplit(b"\r\n\r\n", 1)[-1].decode())
PY"""


def main() -> None:
    runtime = os.environ.get("AKERNEL_TEST_RUNTIME", "runsc")
    with Sandbox(
        runtime=runtime,
        cpu=2000,
        memory=4096,
        storage_mb=1024,
        failover=True,
    ) as sandbox:
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
