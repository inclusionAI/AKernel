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

import contextlib
import io
import unittest
from unittest.mock import MagicMock, patch

from akernel_sdk import cli
from akernel_sdk._addresses import Endpoint


class CliTest(unittest.TestCase):
    def test_resources_display_xpu_allocatable_and_capacity(self):
        output = io.StringIO()
        response = {
            "resource": {
                "fragment": {
                    "node-1": {
                        "id": "node-1",
                        "capacity": {
                            "resources": {
                                "CPU": {"scalar": {"value": 4000}},
                                "Memory": {"scalar": {"value": 8192}},
                                "GPU/l20": {
                                    "vectors": {
                                        "values": {
                                            "count": {
                                                "vectors": {
                                                    "node-1": {"values": [1, 1]}
                                                }
                                            }
                                        }
                                    }
                                },
                            }
                        },
                        "allocatable": {
                            "resources": {
                                "CPU": {"scalar": {"value": 3000}},
                                "Memory": {"scalar": {"value": 4096}},
                                "GPU/l20": {
                                    "vectors": {
                                        "values": {
                                            "count": {
                                                "vectors": {
                                                    "node-1": {"values": [0, 1]}
                                                }
                                            }
                                        }
                                    }
                                },
                            }
                        },
                    }
                }
            }
        }
        with (
            patch("akernel_sdk.cli.query_resource_view", return_value=response),
            contextlib.redirect_stdout(output),
        ):
            cli.handle_resources()

        self.assertIn("XPU", output.getvalue())
        self.assertIn("gpu/l20 1/2", output.getvalue())

    def test_delete_uses_frontend_actor_api(self):
        output = io.StringIO()
        endpoint = Endpoint("akernel.example", 443, "https", False)
        with (
            patch("akernel_sdk.cli._get_endpoint", return_value=endpoint),
            patch("akernel_sdk.cli._get_auth_token", return_value="token"),
            patch("akernel_sdk.cli._create_ssl_context"),
            patch(
                "akernel_sdk.cli._make_json_request",
                return_value={"status": 200, "body": '{"code":0,"message":""}'},
            ) as make_request,
        ):
            with contextlib.redirect_stdout(output):
                cli.handle_delete(["sandbox-1", "sandbox-2"])

        self.assertEqual(
            [call.args[3] for call in make_request.call_args_list],
            [
                {"instanceID": "sandbox-1", "signal": 1},
                {"instanceID": "sandbox-2", "signal": 1},
            ],
        )
        self.assertTrue(
            all(
                call.args[0] == "https://akernel.example/frontend/v1/instance/kill"
                for call in make_request.call_args_list
            )
        )
        self.assertEqual(
            output.getvalue().splitlines(),
            ["deleted: sandbox-1", "deleted: sandbox-2"],
        )

    def test_delete_reports_failures_and_continues(self):
        stderr = io.StringIO()
        responses = [
            {"status": 200, "body": '{"code":22,"message":"not found"}'},
            {"status": 200, "body": '{"code":0,"message":""}'},
        ]
        with (
            patch(
                "akernel_sdk.cli._get_endpoint",
                return_value=Endpoint("akernel.example", 443, "https", False),
            ),
            patch("akernel_sdk.cli._get_auth_token", return_value="token"),
            patch("akernel_sdk.cli._create_ssl_context"),
            patch("akernel_sdk.cli._make_json_request", side_effect=responses),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.handle_delete(["missing", "sandbox-2"])

        self.assertEqual(raised.exception.code, 1)
        self.assertIn("server returned code 22: not found", stderr.getvalue())

    def test_endpoint_errors_are_reported(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                cli._get_endpoint(lambda: (_ for _ in ()).throw(RuntimeError("bad")))
        self.assertIn("Error: bad", stderr.getvalue())

    @patch("signal.signal", return_value=object())
    @patch("akernel_sdk.cli.Pty")
    def test_exec_returns_remote_exit_code(self, pty_type, _signal):
        session = MagicMock()
        session.done = True
        session.wait.return_value = 7
        pty_type.return_value.create.return_value = session
        stdin = MagicMock()
        stdin.fileno.return_value = 0
        stdin.isatty.return_value = False

        with patch("sys.stdin", stdin):
            result = cli.handle_exec("sandbox-1", ["/bin/bash", "-l"])

        self.assertEqual(result, 7)
        pty_type.assert_called_once_with("sandbox-1")
        pty_type.return_value.create.assert_called_once_with(
            ["/bin/bash", "-l"],
            rows=unittest.mock.ANY,
            cols=unittest.mock.ANY,
            on_data=cli._write_terminal_data,
        )
        session.wait.assert_called_once_with()

    @patch("signal.signal", return_value=object())
    @patch("select.select", return_value=([0], [], []))
    @patch("akernel_sdk.cli.os.read", return_value=b"\x1d")
    @patch("akernel_sdk.cli.Pty")
    def test_exec_escape_sequence_closes_session(
        self,
        pty_type,
        _read,
        _select,
        _signal,
    ):
        session = MagicMock()
        session.done = False
        pty_type.return_value.create.return_value = session
        stdin = MagicMock()
        stdin.fileno.return_value = 0
        stdin.isatty.return_value = False

        with patch("sys.stdin", stdin):
            result = cli.handle_exec("sandbox-1", ["/bin/bash"])

        self.assertEqual(result, 0)
        session.close.assert_called_once_with()
        session.send_stdin.assert_not_called()

    @patch("signal.signal", return_value=object())
    @patch("select.select", return_value=([0], [], []))
    @patch("akernel_sdk.cli.os.read", return_value=b"")
    @patch("akernel_sdk.cli.Pty")
    def test_exec_stdin_eof_waits_for_remote_exit(
        self,
        pty_type,
        _read,
        _select,
        _signal,
    ):
        session = MagicMock()
        session.done = False
        session.wait.return_value = 9
        pty_type.return_value.create.return_value = session
        stdin = MagicMock()
        stdin.fileno.return_value = 0
        stdin.isatty.return_value = False

        with patch("sys.stdin", stdin):
            result = cli.handle_exec("sandbox-1", ["/bin/sh", "-c", "exit 9"])

        self.assertEqual(result, 9)
        session.close_stdin.assert_called_once_with()
        session.send_stdin.assert_not_called()
        session.wait.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
