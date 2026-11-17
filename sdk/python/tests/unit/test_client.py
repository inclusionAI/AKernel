"""Explicit clients must never leak credentials or configuration globally."""

import os
import unittest
from unittest.mock import MagicMock, patch

from akernel_sdk import Client, Sandbox
from akernel_sdk.types import SandboxInfo


class ClientTest(unittest.TestCase):
    def backend(self, identity):
        backend = MagicMock()
        session = backend.create.return_value
        session.id = identity
        session.get_info.return_value = SandboxInfo(
            identity, "running", 1000, 1024, "python"
        )
        return backend

    def test_two_clients_isolate_credentials_gateway_and_resource_factories(self):
        first, second, default = (self.backend(n) for n in ("a", "b", "default"))
        before = dict(os.environ)
        with (
            patch(
                "akernel_sdk._client.new_backend", side_effect=[first, second]
            ) as factory,
            patch("akernel_sdk.sandbox.load_backend", return_value=default),
        ):
            a = Client(
                api_url="https://api-a:8443",
                api_key="test-a",
                gateway_address="https://gateway-a",
            )
            b = Client(
                server_address="https://api-b",
                token="test-b",
                gateway_address="http://gateway-b:8080",
            )
            with a.Sandbox.create(image="python", port_forwardings=[8080]) as sa:
                with b.Sandbox.create(image="python", port_forwardings=[8080]) as sb:
                    with Sandbox() as sd:
                        self.assertEqual((sa.id, sb.id, sd.id), ("a", "b", "default"))
                        self.assertEqual(
                            sa.get_port_url(8080), "https://gateway-a/a/8080"
                        )
                        self.assertEqual(
                            sb.get_port_url(8080), "http://gateway-b:8080/b/8080"
                        )
            configs = [call.args[0] for call in factory.call_args_list]
            self.assertEqual([c.token for c in configs], ["test-a", "test-b"])
            self.assertEqual([c.api_endpoint.host for c in configs], ["api-a", "api-b"])
            self.assertNotIn("test-a", repr(configs[0]))
            self.assertEqual(dict(os.environ), before)

    def test_invalid_aliases_and_credentials_fail_before_backend(self):
        with patch("akernel_sdk._client.new_backend") as factory:
            for options in (
                {
                    "server_address": "https://api",
                    "api_url": "https://other",
                    "token": "test",
                },
                {"server_address": "https://api", "token": "test", "api_key": "other"},
                {"server_address": "https://api", "token": ""},
                {"server_address": 42, "token": "test"},
                {"server_address": "https://api/prefix", "token": "test"},
                {"server_address": "https://user:secret@api", "token": "test"},
                {"server_address": "ftp://api", "token": "test"},
                {"server_address": "https://api?key=bad", "token": "test"},
                {"server_address": "https://api", "token": "test", "verify_tls": 1},
            ):
                with (
                    self.subTest(options=options),
                    self.assertRaises((TypeError, ValueError)),
                ):
                    Client(**options)
            factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
