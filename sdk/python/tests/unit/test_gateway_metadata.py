# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0

import json
import unittest
from unittest.mock import MagicMock, patch

from akernel_sdk import sandbox
from akernel_sdk._addresses import Endpoint


class GatewayMetadataTest(unittest.TestCase):
    def setUp(self):
        sandbox._gateway_internal_address_cache.clear()
        self.addCleanup(sandbox._gateway_internal_address_cache.clear)

    def resolve(self, payload, gateway):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        with patch.object(sandbox.urllib.request, "urlopen", return_value=response):
            return sandbox._get_gateway_internal_address(gateway)

    @patch.object(sandbox, "api_endpoint_from_env",
                  return_value=Endpoint("public.example", 443, "https", False))
    def test_internal_ports_follow_listener_instead_of_service_port(self, _server):
        payload = {"pod_ip": "10.0.0.4", "http_port": 8080, "https_port": 8443}
        for scheme, expected in (("http", 8080), ("https", 8443)):
            with self.subTest(scheme=scheme):
                gateway = Endpoint("public.example", 10000, scheme, True)
                self.assertEqual(self.resolve(payload, gateway), ("10.0.0.4", expected))

    @patch.object(sandbox, "api_endpoint_from_env",
                  return_value=Endpoint("public.example", 443, "https", False))
    def test_legacy_metadata_uses_configured_gateway_port(self, _server):
        gateway = Endpoint("public.example", 8888, "http", True)
        self.assertEqual(self.resolve({"pod_ip": "10.0.0.4"}, gateway),
                         ("10.0.0.4", 8888))

    @patch.object(sandbox, "api_endpoint_from_env")
    def test_cache_is_scoped_to_api_endpoint(self, server):
        gateway = Endpoint("public.example", 80, "http", False)
        for index in (1, 2):
            server.return_value = Endpoint(f"cluster{index}", 443, "https", False)
            ip = f"10.0.0.{index}"
            self.assertEqual(self.resolve({"pod_ip": ip}, gateway), (ip, 80))

    @patch.object(sandbox, "api_endpoint_from_env",
                  return_value=Endpoint("public.example", 443, "https", False))
    def test_invalid_port_is_rejected(self, _server):
        gateway = Endpoint("public.example", 80, "http", False)
        for port in (0, 65536, True, "8080"):
            with self.subTest(port=port), self.assertRaises(RuntimeError):
                self.resolve({"pod_ip": "10.0.0.4", "http_port": port}, gateway)
