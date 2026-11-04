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

import io
import unittest
from unittest.mock import patch

from tests.e2e.full import test_tenant_isolation as fixture


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class TenantIsolationFixtureTest(unittest.TestCase):
    @patch("tests.e2e.full.test_tenant_isolation.api_endpoint_from_env")
    @patch("tests.e2e.full.test_tenant_isolation.urllib.request.urlopen")
    def test_instance_list_follows_paginated_response(self, urlopen, endpoint):
        endpoint.return_value.base_url.return_value = "https://adx.example"
        endpoint.return_value.use_tls = False
        urlopen.side_effect = [
            _Response(b'{"items":[{"id":"one"}],"nextPageToken":"page/2"}'),
            _Response(b'{"items":[{"id":"two"}],"nextPageToken":""}'),
        ]

        self.assertEqual(
            fixture._list_instances("tenant-key"),
            [{"id": "one"}, {"id": "two"}],
        )
        self.assertEqual(
            urlopen.call_args_list[0].args[0].full_url,
            "https://adx.example/api/instances?pageSize=1000",
        )
        self.assertEqual(
            urlopen.call_args_list[1].args[0].full_url,
            "https://adx.example/api/instances?pageSize=1000&pageToken=page%2F2",
        )


if __name__ == "__main__":
    unittest.main()
