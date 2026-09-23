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

import json
import os
import unittest
from unittest.mock import MagicMock, patch

from akernel_sdk import _resource_api, _resources
from akernel_sdk._addresses import Endpoint


class ResourcesTest(unittest.TestCase):
    def _query(self, payload):
        with patch.object(_resources, "query_resource_view", return_value=payload):
            return _resources.resources()

    def test_current_public_resource_response_is_backend_neutral(self):
        result = self._query(
            {
                "items": [
                    {
                        "id": "node-1",
                        "status": 1,
                        "capacity": {"CPU": 8000, "Memory": 16384},
                        "allocatable": {"CPU": 6000, "Memory": 12288},
                        "labels": {"NODE_ID": "node-1"},
                    }
                ]
            }
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, "node-1")
        self.assertEqual(result[0].status, 1)
        self.assertEqual(result[0].capacity["Memory"], 16384.0)
        self.assertEqual(result[0].allocatable["CPU"], 6000.0)
        self.assertEqual(result[0].labels["NODE_ID"], ["node-1"])

    def test_query_uses_the_public_resource_route(self):
        response = MagicMock()
        response.read.return_value = json.dumps({"items": []}).encode()
        context = MagicMock()
        context.__enter__.return_value = response
        with (
            patch.dict(os.environ, {"AKERNEL_TOKEN": "token"}, clear=True),
            patch.object(
                _resource_api,
                "api_endpoint_from_env",
                return_value=Endpoint("api.example", 443, "https", False),
            ),
            patch.object(
                _resource_api.request,
                "urlopen",
                return_value=context,
            ) as urlopen,
        ):
            self.assertEqual(_resource_api.query_resource_view(), {"items": []})

        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://api.example/api/sandbox/v1/resources",
        )

    def test_frontend_resource_values_are_backend_neutral(self):
        payload = {
            "resource": {
                "fragment": {
                    "node-1": {
                        "id": "node-1",
                        "status": 0,
                        "capacity": {
                            "resources": {
                                "CPU": {"scalar": {"value": 8000}},
                            }
                        },
                        "allocatable": {
                            "resources": {
                                "CPU": {"scalar": {"value": 6000}},
                            }
                        },
                        "nodeLabels": {
                            "HOST_IP": {"items": {"192.0.2.10": 1}},
                        },
                    }
                }
            }
        }
        result = self._query(payload)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, "node-1")
        self.assertEqual(result[0].capacity["CPU"], 8000.0)
        self.assertEqual(result[0].allocatable["CPU"], 6000.0)
        self.assertEqual(result[0].labels["HOST_IP"], ["192.0.2.10"])

    def test_empty_fragment_falls_back_to_aggregate_resource(self):
        payload = {
            "resource": {
                "id": "domain-scheduler",
                "fragment": {},
                "status": 0,
                "capacity": {
                    "resources": {"CPU": {"scalar": {"value": 8000}}}
                },
                "allocatable": {
                    "resources": {"CPU": {"scalar": {"value": 6000}}}
                },
            }
        }

        result = self._query(payload)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, "domain-scheduler")
        self.assertEqual(result[0].capacity["CPU"], 8000.0)
        self.assertEqual(result[0].allocatable["CPU"], 6000.0)

    def test_token_is_required_without_loading_a_backend(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(RuntimeError, "AKERNEL_TOKEN"),
        ):
            _resources.resources()


if __name__ == "__main__":
    unittest.main()
