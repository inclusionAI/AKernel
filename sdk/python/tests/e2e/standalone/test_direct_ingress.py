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

"""Exercise the ADX HTTPS control and HTTP data listeners directly."""

import os
import ssl
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

from akernel_sdk import Sandbox
from akernel_sdk._addresses import Endpoint, api_endpoint_from_env

_ENABLED = (
    os.environ.get("AKERNEL_RUN_INTEGRATION") == "1"
    and bool(os.environ.get("AKERNEL_SERVER_ADDRESS"))
    and bool(os.environ.get("AKERNEL_TOKEN"))
)


@unittest.skipUnless(_ENABLED, "set AKERNEL_RUN_INTEGRATION=1 and SDK credentials")
class DirectIngressIntegrationTest(unittest.TestCase):
    def test_https_api_requires_a_key_and_accepts_the_sdk_key(self):
        endpoint = api_endpoint_from_env()
        self.assertTrue(endpoint.use_tls)
        opener = build_opener(
            ProxyHandler({}), HTTPSHandler(context=ssl._create_unverified_context())
        )
        url = endpoint.base_url() + "/api/environment/v1/resources"
        with self.assertRaises(HTTPError) as denied:
            opener.open(url, timeout=10)
        self.assertEqual(denied.exception.code, 401)
        request = Request(url, headers={"X-Auth": os.environ["AKERNEL_TOKEN"].strip()})
        with opener.open(request, timeout=10) as response:
            self.assertEqual(response.status, 200)

    def test_pty_output_resize_and_exit_through_the_control_listener(self):
        output = bytearray()
        with Sandbox(
            runtime=os.environ.get("AKERNEL_TEST_RUNTIME", "runsc"),
            cpu=1000,
            memory=2048,
        ) as sandbox:
            with sandbox.pty.create(on_data=output.extend) as session:
                session.resize(rows=40, cols=120)
                session.send_stdin(b"printf 'DIRECT_PTY_OK\\n'\nstty size\nexit 7\n")
                self.assertEqual(session.wait(timeout=30), 7)
            self.assertIn(b"DIRECT_PTY_OK", output)
            self.assertIn(b"40 120", output)

    def test_http_data_port_serves_the_guest_payload(self):
        api = api_endpoint_from_env()
        endpoint = Endpoint(
            host=api.host,
            port=int(os.environ.get("AKERNEL_TEST_HTTP_DATA_PORT", "80")),
            scheme="http",
            explicit_port=True,
        )
        self.assertNotEqual(endpoint.port, api.port)
        image = os.environ.get("AKERNEL_TEST_HTTP_IMAGE", "python:3.12-slim")
        port = 18081
        opener = build_opener(ProxyHandler({}))
        with Sandbox(
            image=image,
            runtime=os.environ.get("AKERNEL_TEST_RUNTIME", "runsc"),
            cpu=1000,
            memory=2048,
            port_forwardings=[port],
        ) as sandbox:
            sandbox.files.write("/tmp/index.html", "DIRECT_DATA_OK\n")
            server = sandbox.commands.run(
                f"python3 -m http.server {port} --bind 0.0.0.0 --directory /tmp",
                background=True,
            )
            try:
                deadline = time.monotonic() + 30
                while True:
                    try:
                        with opener.open(
                            f"{endpoint.base_url()}/{sandbox.id}/{port}", timeout=5
                        ) as response:
                            self.assertEqual(response.read(), b"DIRECT_DATA_OK\n")
                        break
                    except (HTTPError, URLError, TimeoutError):
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.2)
            finally:
                server.kill()


if __name__ == "__main__":
    unittest.main()
