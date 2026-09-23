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
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

from akernel_sdk._addresses import Endpoint
from akernel_sdk._pty_transport import (
    _build_pty_uri,
    _PtyConnection,
    _PtyTransportError,
)
from akernel_sdk.pty import Pty, PtyError, PtySession, _normalize_command


class PtyTest(unittest.TestCase):
    def test_command_normalization_preserves_quoted_arguments(self):
        self.assertEqual(
            _normalize_command("/bin/bash -lc 'printf hello world'"),
            ["/bin/bash", "-lc", "printf hello world"],
        )
        self.assertEqual(_normalize_command(["python3", "-i"]), ["python3", "-i"])
        with self.assertRaises(ValueError):
            _normalize_command("")

    def test_uri_uses_repeated_command_arguments_and_protocol(self):
        uri = _build_pty_uri(
            Endpoint("akernel.example", 443, "https", False),
            instance_id="sandbox-1",
            token="token.value",
            command=["/bin/bash", "-lc", "printf hello world"],
            rows=30,
            cols=100,
        )
        parsed = urlparse(uri)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, "wss")
        self.assertEqual(parsed.netloc, "akernel.example")
        self.assertEqual(query["command"], ["/bin/bash", "-lc", "printf hello world"])
        self.assertEqual(query["protocol"], ["sandbox.pty.v1"])
        self.assertEqual(query["rows"], ["30"])
        self.assertEqual(query["cols"], ["100"])

    def test_control_events_complete_connection(self):
        connection = _PtyConnection(
            "ws://example.invalid",
            ssl_context=None,
            rows=24,
            cols=80,
            on_data=None,
            on_done=lambda: None,
        )
        connection._handle_control(
            json.dumps(
                {"version": 1, "type": "started", "session_id": "session-1"}
            )
        )
        self.assertEqual(connection.session_id, "session-1")
        connection._handle_control(
            json.dumps(
                {
                    "version": 1,
                    "type": "exited",
                    "session_id": "session-1",
                    "exit_code": 7,
                }
            )
        )
        self.assertTrue(connection.done)
        self.assertEqual(connection.wait(0.1), 7)

    def test_remote_error_is_reported(self):
        connection = _PtyConnection(
            "ws://example.invalid",
            ssl_context=None,
            rows=24,
            cols=80,
            on_data=None,
            on_done=lambda: None,
        )
        connection._handle_control(
            json.dumps(
                {
                    "version": 1,
                    "type": "error",
                    "session_id": "session-2",
                    "message": "cannot start",
                }
            )
        )
        with self.assertRaisesRegex(_PtyTransportError, "cannot start"):
            connection.wait(0.1)

    def test_public_session_delegates_and_wraps_errors(self):
        connection = MagicMock()
        connection.session_id = "session-3"
        connection.exit_code = None
        connection.done = False
        connection.wait.return_value = 3
        removed = []
        session = PtySession(connection, remove=removed.append)

        session.send_stdin(b"hello")
        session.close_stdin()
        session.resize(rows=40, cols=120)
        self.assertEqual(session.wait(timeout=1), 3)
        session.close()

        connection.send_stdin.assert_called_once_with(b"hello")
        connection.close_stdin.assert_called_once_with()
        connection.resize.assert_called_once_with(rows=40, cols=120)
        self.assertEqual(removed, [session])

        connection.wait.side_effect = _PtyTransportError("broken")
        with self.assertRaisesRegex(PtyError, "broken"):
            session.wait(timeout=1)

    @patch.dict(
        "os.environ",
        {
            "AKERNEL_TOKEN": "token",
            "AKERNEL_SERVER_ADDRESS": "127.0.0.1:8080",
        },
        clear=False,
    )
    @patch("akernel_sdk.pty._PtyConnection")
    def test_manager_waits_for_started_connection(self, connection_type):
        connection = connection_type.return_value
        connection.session_id = "session-4"
        session = Pty("sandbox-4").create(["/bin/bash"], timeout=2)

        connection.start.assert_called_once_with(2.0)
        self.assertEqual(session.session_id, "session-4")

    def test_manager_delegates_to_backend_native_pty(self):
        driver = MagicMock()
        native = MagicMock()
        native.session_id = "session-native"
        driver.create.return_value = native

        session = Pty("sandbox-native", driver=driver).create(
            ["/bin/bash", "-lc", "printf ok"],
            rows=40,
            cols=120,
            timeout=3,
        )

        driver.create.assert_called_once_with(
            ["/bin/bash", "-lc", "printf ok"],
            rows=40,
            cols=120,
            on_data=None,
            timeout=3,
        )
        self.assertEqual(session.session_id, "session-native")
        session.close()
        native.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
