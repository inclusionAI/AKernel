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

"""Exercise a real user HTTP server through the published sandbox port."""

import os
import time
import unittest
import urllib.error
import urllib.request
from contextlib import ExitStack
from urllib.parse import urlsplit, urlunsplit

from akernel_sdk import Sandbox

_ENABLED = (
    os.environ.get("AKERNEL_RUN_INTEGRATION") == "1"
    and bool(os.environ.get("AKERNEL_SERVER_ADDRESS"))
    and bool(os.environ.get("AKERNEL_TOKEN"))
)
_IMAGE = os.environ.get("AKERNEL_TEST_HTTP_IMAGE")
_RUNTIME = os.environ.get("AKERNEL_TEST_RUNTIME", "runsc")
_HOST_DOMAIN = os.environ.get("AKERNEL_TEST_PORT_HOST_DOMAIN", "").strip().strip(".")


@unittest.skipUnless(_ENABLED, "set AKERNEL_RUN_INTEGRATION=1 and SDK credentials")
class PortForwardIntegrationTest(unittest.TestCase):
    @staticmethod
    def _await_body(url: str, expected: bytes) -> None:
        deadline = time.monotonic() + 30
        while True:
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    body = response.read()
                    if body != expected:
                        raise AssertionError(
                            "port route returned another sandbox's body"
                        )
                    return
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.5)

    @staticmethod
    def _host_request(gateway_url: str, host: str, path: str) -> urllib.request.Request:
        endpoint = urlsplit(gateway_url)
        url = urlunsplit((endpoint.scheme, endpoint.netloc, path, "", ""))
        return urllib.request.Request(url, headers={"Host": host})

    @unittest.skipUnless(
        _IMAGE and _HOST_DOMAIN,
        "set AKERNEL_TEST_HTTP_IMAGE and AKERNEL_TEST_PORT_HOST_DOMAIN",
    )
    def test_host_subdomains_route_same_port_to_distinct_sandboxes(self):
        port = 18081
        with ExitStack() as stack:
            routes = []
            for sequence in range(2):
                sandbox = stack.enter_context(
                    Sandbox(
                        image=_IMAGE,
                        runtime=_RUNTIME,
                        cpu=1000,
                        memory=2048,
                        port_forwardings=[port],
                    )
                )
                payload = f"HOST_ROUTE_{sequence}_{sandbox.id}\n".encode()
                sandbox.files.write("/tmp/host-route.txt", payload)
                server = sandbox.commands.run(
                    f"python3 -m http.server {port} --bind 0.0.0.0 --directory /tmp",
                    background=True,
                )
                stack.callback(server.kill)
                host = f"{sandbox.id}-{port}.{_HOST_DOMAIN}"
                self.assertLessEqual(len(host.split(".", maxsplit=1)[0]), 63)
                routes.append(
                    (
                        self._host_request(
                            sandbox.get_port_url(port), host, "/host-route.txt"
                        ),
                        payload,
                    )
                )

            for request, expected in routes:
                deadline = time.monotonic() + 30
                while True:
                    try:
                        with urllib.request.urlopen(request, timeout=5) as response:
                            self.assertEqual(response.read(), expected)
                        break
                    except (
                        urllib.error.HTTPError,
                        urllib.error.URLError,
                        TimeoutError,
                    ):
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.5)

    @unittest.skipUnless(
        _IMAGE, "set AKERNEL_TEST_HTTP_IMAGE to a resolvable OCI image"
    )
    def test_declared_port_reaches_the_correct_sandbox(self):
        port = 18081
        with Sandbox(
            image=_IMAGE,
            runtime=_RUNTIME,
            cpu=1000,
            memory=2048,
            port_forwardings=[port],
        ) as sandbox:
            sandbox.files.write("/tmp/index.html", "PORT_FORWARD_E2E_OK\n")
            server = sandbox.commands.run(
                f"python3 -m http.server {port} --bind 0.0.0.0 --directory /tmp",
                background=True,
            )
            try:
                url = sandbox.get_port_url(port)
                deadline = time.monotonic() + 30
                last_error = None
                while time.monotonic() < deadline:
                    try:
                        with urllib.request.urlopen(url, timeout=5) as response:
                            self.assertEqual(response.read(), b"PORT_FORWARD_E2E_OK\n")
                            return
                    except (
                        urllib.error.HTTPError,
                        urllib.error.URLError,
                        TimeoutError,
                    ) as error:
                        last_error = error
                        time.sleep(0.5)
                self.fail(f"declared port did not become reachable: {last_error}")
            finally:
                server.kill()

    @unittest.skipUnless(
        _IMAGE, "set AKERNEL_TEST_HTTP_IMAGE to a resolvable OCI image"
    )
    def test_two_sandboxes_with_same_guest_port_keep_routes_isolated(self):
        port = 18081
        with ExitStack() as stack:
            urls = []
            payloads = []
            for sequence in range(2):
                sandbox = stack.enter_context(
                    Sandbox(
                        image=_IMAGE,
                        runtime=_RUNTIME,
                        cpu=1000,
                        memory=2048,
                        port_forwardings=[port],
                    )
                )
                payload = f"PORT_ROUTE_{sequence}_{sandbox.id}\n".encode()
                sandbox.files.write("/tmp/index.html", payload)
                server = sandbox.commands.run(
                    f"python3 -m http.server {port} --bind 0.0.0.0 --directory /tmp",
                    background=True,
                )
                stack.callback(server.kill)
                urls.append(sandbox.get_port_url(port))
                payloads.append(payload)

            self.assertNotEqual(urls[0], urls[1])
            for url, payload in zip(urls, payloads, strict=True):
                self._await_body(url, payload)
            for _ in range(10):
                for url, payload in zip(urls, payloads, strict=True):
                    with urllib.request.urlopen(url, timeout=5) as response:
                        self.assertEqual(response.read(), payload)

    @unittest.skipUnless(
        _IMAGE, "set AKERNEL_TEST_HTTP_IMAGE to a resolvable OCI image"
    )
    def test_deleted_route_is_withdrawn_while_other_sandbox_remains_reachable(self):
        port = 18081
        with Sandbox(
            image=_IMAGE,
            runtime=_RUNTIME,
            cpu=1000,
            memory=2048,
            port_forwardings=[port],
        ) as survivor:
            survivor_payload = f"SURVIVOR_{survivor.id}\n".encode()
            survivor.files.write("/tmp/index.html", survivor_payload)
            survivor_server = survivor.commands.run(
                f"python3 -m http.server {port} --bind 0.0.0.0 --directory /tmp",
                background=True,
            )
            try:
                survivor_url = survivor.get_port_url(port)
                self._await_body(survivor_url, survivor_payload)
                with Sandbox(
                    image=_IMAGE,
                    runtime=_RUNTIME,
                    cpu=1000,
                    memory=2048,
                    port_forwardings=[port],
                ) as removed:
                    removed_payload = f"REMOVED_{removed.id}\n".encode()
                    removed.files.write("/tmp/index.html", removed_payload)
                    removed_server = removed.commands.run(
                        f"python3 -m http.server {port} "
                        "--bind 0.0.0.0 --directory /tmp",
                        background=True,
                    )
                    try:
                        removed_url = removed.get_port_url(port)
                        self._await_body(removed_url, removed_payload)
                    finally:
                        removed_server.kill()

                deadline = time.monotonic() + 15
                while True:
                    try:
                        with urllib.request.urlopen(removed_url, timeout=5) as response:
                            body = response.read()
                        self.assertNotEqual(body, survivor_payload)
                    except urllib.error.HTTPError as error:
                        if error.code in {404, 503}:
                            break
                    except (urllib.error.URLError, TimeoutError):
                        pass
                    if time.monotonic() >= deadline:
                        self.fail("deleted sandbox route remained published")
                    time.sleep(0.2)
                self._await_body(survivor_url, survivor_payload)
            finally:
                survivor_server.kill()


if __name__ == "__main__":
    unittest.main()
