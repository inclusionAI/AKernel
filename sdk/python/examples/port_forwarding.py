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

"""Expose an HTTP server running in a sandbox through AKernel's gateway."""

import os
import time
import urllib.request

from akernel_sdk import Sandbox

PORT = 8080
IMAGE = os.environ.get("AKERNEL_PORT_FORWARDING_IMAGE", "python:3.12-slim")


def fetch_with_retry(url: str, timeout: float = 30.0) -> str:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                return response.read().decode().strip()
        except Exception as error:
            last_error = error
            time.sleep(1)
    raise RuntimeError(f"port forwarding request failed: {last_error!r}")


def main() -> None:
    with Sandbox(
        image=IMAGE,
        cpu=1000,
        memory=2048,
        port_forwardings=[PORT],
    ) as sandbox:
        sandbox.files.write("/tmp/index.html", "AKERNEL_PORT_FORWARD_OK\n")
        server = sandbox.commands.run(
            f"python3 -m http.server {PORT} --bind 0.0.0.0 --directory /tmp",
            background=True,
        )
        url = sandbox.get_port_url(PORT)
        body = fetch_with_retry(url)
        assert body == "AKERNEL_PORT_FORWARD_OK", body
        print(f"{url} -> {body}")
        server.kill()


if __name__ == "__main__":
    main()
