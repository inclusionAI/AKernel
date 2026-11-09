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

from akernel_sdk import Sandbox
from akernel_sdk._addresses import (
    api_endpoint_from_env,
    exec_endpoint_from_env,
    gateway_endpoint_from_env,
)
from akernel_sdk._backends import registry


def endpoint_tuple(endpoint):
    return endpoint.scheme, endpoint.host, endpoint.port, endpoint.use_tls


class AddressConfigTest(unittest.TestCase):
    def test_host_only_uses_separate_control_and_data_ports(self):
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

    def test_explicit_server_port_preserves_shared_port_contract(self):
        with patch.dict(
            os.environ, {"AKERNEL_SERVER_ADDRESS": "10.0.0.1:8888"}, clear=True
        ):
            expected = ("https", "10.0.0.1", 8888, True)
            self.assertEqual(endpoint_tuple(api_endpoint_from_env()), expected)
            self.assertEqual(endpoint_tuple(exec_endpoint_from_env()), expected)
            self.assertEqual(
                endpoint_tuple(gateway_endpoint_from_env()),
                ("http", "10.0.0.1", 8888, False),
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

    def test_gateway_override_uses_scheme_default_port(self):
        cases = (
            ("https://gw.example.com", "https", 443, True),
            ("wss://gw.example.com", "wss", 443, True),
            ("http://gw.example.com", "http", 80, False),
            ("ws://gw.example.com", "ws", 80, False),
            ("gw.example.com", "http", 80, False),
            ("https://gw.example.com:80", "https", 80, True),
        )
        for address, scheme, port, tls in cases:
            with (
                self.subTest(address=address),
                patch.dict(
                    os.environ,
                    {"AKERNEL_GATEWAY_ADDRESS": address},
                    clear=True,
                ),
            ):
                endpoint = gateway_endpoint_from_env()
                self.assertEqual(
                    endpoint_tuple(endpoint), (scheme, "gw.example.com", port, tls)
                )
                if address == "https://gw.example.com":
                    self.assertEqual(endpoint.base_url(), address)

    def test_missing_server_address_is_clear(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "AKERNEL_SERVER_ADDRESS"):
                api_endpoint_from_env()

    def test_unexpanded_variables_are_rejected_in_control_and_gateway_addresses(self):
        for address in (
            "$AKERNEL_SERVER_ADDRESS",
            "${AKERNEL_SERVER_ADDRESS}",
            "https://$AKERNEL_SERVER_ADDRESS:443",
        ):
            for setting, resolve in (
                ("AKERNEL_SERVER_ADDRESS", api_endpoint_from_env),
                ("AKERNEL_GATEWAY_ADDRESS", gateway_endpoint_from_env),
            ):
                with (
                    self.subTest(setting=setting, address=address),
                    patch.dict(os.environ, {setting: address}, clear=True),
                ):
                    with self.assertRaisesRegex(RuntimeError, "unexpanded"):
                        resolve()

    def test_invalid_gateway_fails_before_backend_creation(self):
        with (
            patch.dict(
                os.environ,
                {
                    "AKERNEL_SERVER_ADDRESS": "https://api.example",
                    "AKERNEL_GATEWAY_ADDRESS": "$AKERNEL_SERVER_ADDRESS",
                    "AKERNEL_TOKEN": "test-token",
                },
                clear=True,
            ),
            patch.object(registry, "_loaded_backend", None),
            patch.object(registry, "_selected_backend", "adx"),
            patch.object(registry, "_is_installed", return_value=True),
            patch.object(registry, "new_backend") as create,
        ):
            with self.assertRaisesRegex(RuntimeError, "unexpanded"):
                Sandbox(image="ubuntu:24.04")
        create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
