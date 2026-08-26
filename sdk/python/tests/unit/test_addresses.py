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

import os
import unittest
from unittest.mock import patch

from akernel_sdk._addresses import (
    api_endpoint_from_env,
    exec_endpoint_from_env,
    gateway_endpoint_from_env,
)


def endpoint_tuple(endpoint):
    return endpoint.scheme, endpoint.host, endpoint.port, endpoint.use_tls


class AddressConfigTest(unittest.TestCase):
    def test_host_only_uses_https_api_and_http_gateway(self):
        with patch.dict(os.environ, {"AKERNEL_SERVER_ADDRESS": "10.0.0.1"}, clear=True):
            self.assertEqual(
                endpoint_tuple(api_endpoint_from_env()),
                ("https", "10.0.0.1", 443, True),
            )
            self.assertEqual(
                endpoint_tuple(exec_endpoint_from_env()),
                ("https", "10.0.0.1", 443, True),
            )
            self.assertEqual(
                endpoint_tuple(gateway_endpoint_from_env()),
                ("http", "10.0.0.1", 80, False),
            )

    def test_explicit_server_port_uses_plain_http_gateway(self):
        with patch.dict(
            os.environ, {"AKERNEL_SERVER_ADDRESS": "10.0.0.1:8888"}, clear=True
        ):
            expected = ("https", "10.0.0.1", 8888, True)
            gateway_expected = ("http", "10.0.0.1", 8888, False)
            self.assertEqual(endpoint_tuple(api_endpoint_from_env()), expected)
            self.assertEqual(endpoint_tuple(exec_endpoint_from_env()), expected)
            self.assertEqual(
                endpoint_tuple(gateway_endpoint_from_env()), gateway_expected
            )

    def test_gateway_override_only_affects_public_gateway(self):
        with patch.dict(
            os.environ,
            {
                "AKERNEL_SERVER_ADDRESS": "10.0.0.1:8888",
                "AKERNEL_GATEWAY_ADDRESS": "127.0.0.1:8081",
            },
            clear=True,
        ):
            self.assertEqual(
                endpoint_tuple(exec_endpoint_from_env()),
                ("https", "10.0.0.1", 8888, True),
            )
            self.assertEqual(
                endpoint_tuple(gateway_endpoint_from_env()),
                ("http", "127.0.0.1", 8081, False),
            )

    def test_gateway_override_respects_scheme(self):
        with patch.dict(
            os.environ,
            {
                "AKERNEL_SERVER_ADDRESS": "10.0.0.1",
                "AKERNEL_GATEWAY_ADDRESS": "https://gw.example.com:9443",
            },
            clear=True,
        ):
            self.assertEqual(
                endpoint_tuple(gateway_endpoint_from_env()),
                ("https", "gw.example.com", 9443, True),
            )

    def test_internal_yr_gateway_does_not_override_exec_endpoint(self):
        with patch.dict(
            os.environ,
            {
                "AKERNEL_SERVER_ADDRESS": "10.0.0.1",
                "YR_GATEWAY_ADDRESS": "10.0.0.1:80",
            },
            clear=True,
        ):
            self.assertEqual(
                endpoint_tuple(exec_endpoint_from_env()),
                ("https", "10.0.0.1", 443, True),
            )
            self.assertEqual(
                endpoint_tuple(gateway_endpoint_from_env()),
                ("http", "10.0.0.1", 80, False),
            )

    def test_missing_server_address_is_clear(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "AKERNEL_SERVER_ADDRESS"):
                api_endpoint_from_env()


if __name__ == "__main__":
    unittest.main()
