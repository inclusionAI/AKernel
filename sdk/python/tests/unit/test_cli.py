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
    def test_node_status_only_reports_normal_as_ok(self):
        expected = {
            0: "OK",
            1: "EVICTING",
            2: "RECOVERING",
            3: "TO_BE_DELETED",
            9: "9",
            "normal": "OK",
            "evicting": "EVICTING",
            "recovering": "RECOVERING",
            "to-be-deleted": "TO_BE_DELETED",
            "future-state": "future-state",
            None: "-",
        }

        for status, rendered in expected.items():
            with self.subTest(status=status):
                self.assertEqual(cli._fmt_node_status(status), rendered)

    def test_resources_display_xpu_allocatable_and_capacity(self):
        output = io.StringIO()
        response = {
            "resource": {
                "fragment": {
                    "node-1": {
                        "id": "node-1",
                        "status": 1,
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
                    },
                    "node-2": {
                        "id": "node-2",
                        "capacity": {
                            "resources": {
                                "CPU": {"scalar": {"value": 2000}},
                                "Memory": {"scalar": {"value": 4096}},
                            }
                        },
                        "allocatable": {
                            "resources": {
                                "CPU": {"scalar": {"value": 2000}},
                                "Memory": {"scalar": {"value": 4096}},
                            }
                        },
                    },
                }
            }
        }
        with (
            patch("akernel_sdk.cli.query_resource_view", return_value=response),
            contextlib.redirect_stdout(output),
        ):
            cli.handle_resources()

        self.assertIn("XPU", output.getvalue())
        self.assertIn("EVICTING", output.getvalue())
        self.assertIn("OK", output.getvalue())
        self.assertIn("gpu/l20 1/2", output.getvalue())

    def test_list_uses_local_catalog(self):
        output = io.StringIO()
        with (
            patch(
                "akernel_sdk.cli._get_endpoint",
                return_value=Endpoint("akernel.example", 443, "https", False),
            ),
            patch("akernel_sdk.cli._get_auth_token", return_value="token"),
            patch("akernel_sdk.cli._create_ssl_context"),
            patch(
                "akernel_sdk.cli._make_get_request",
                return_value={
                    "status": 200,
                    "body": (
                        '{"items":[{"id":"sandbox-1","status":"running"}],'
                        '"nextPageToken":""}'
                    ),
                },
            ) as make_request,
            contextlib.redirect_stdout(output),
        ):
            cli.handle_list(quiet=True)
        self.assertEqual(
            make_request.call_args.args[0],
            "https://akernel.example/api/instances?pageSize=1000",
        )
        self.assertEqual(output.getvalue().splitlines(), ["sandbox-1"])

    def test_list_reads_all_catalog_pages(self):
        output = io.StringIO()
        responses = [
            {
                "status": 200,
                "body": (
                    '{"items":[{"id":"sandbox-1","status":"running"}],'
                    '"nextPageToken":"next/page"}'
                ),
            },
            {
                "status": 200,
                "body": (
                    '{"items":[{"id":"sandbox-2","status":"running"}],'
                    '"nextPageToken":""}'
                ),
            },
        ]
        with (
            patch(
                "akernel_sdk.cli._get_endpoint",
                return_value=Endpoint("akernel.example", 443, "https", False),
            ),
            patch("akernel_sdk.cli._get_auth_token", return_value="token"),
            patch("akernel_sdk.cli._create_ssl_context"),
            patch(
                "akernel_sdk.cli._make_get_request", side_effect=responses
            ) as make_request,
            contextlib.redirect_stdout(output),
        ):
            cli.handle_list(quiet=True)

        self.assertEqual(output.getvalue().splitlines(), ["sandbox-1", "sandbox-2"])
        self.assertEqual(
            [call.args[0] for call in make_request.call_args_list],
            [
                "https://akernel.example/api/instances?pageSize=1000",
                (
                    "https://akernel.example/api/instances?pageSize=1000"
                    "&pageToken=next%2Fpage"
                ),
            ],
        )

    def test_delete_uses_sandbox_api(self):
        output = io.StringIO()
        endpoint = Endpoint("akernel.example", 443, "https", False)
        with (
            patch("akernel_sdk.cli._get_endpoint", return_value=endpoint),
            patch("akernel_sdk.cli._get_auth_token", return_value="token"),
            patch("akernel_sdk.cli._create_ssl_context"),
            patch(
                "akernel_sdk.cli._make_delete_request",
                return_value={"status": 204, "body": ""},
            ) as make_request,
        ):
            with contextlib.redirect_stdout(output):
                cli.handle_delete(["sandbox-1", "sandbox-2"])

        self.assertEqual(
            [call.args[0] for call in make_request.call_args_list],
            [
                "https://akernel.example/api/sandbox/v1/sandboxes/sandbox-1",
                "https://akernel.example/api/sandbox/v1/sandboxes/sandbox-2",
            ],
        )
        self.assertEqual(
            output.getvalue().splitlines(),
            ["deleted: sandbox-1", "deleted: sandbox-2"],
        )

    def test_delete_reports_failures_and_continues(self):
        stderr = io.StringIO()
        responses = [
            {"status": 503, "body": '{"message":"unavailable"}'},
            {"status": 204, "body": ""},
        ]
        with (
            patch(
                "akernel_sdk.cli._get_endpoint",
                return_value=Endpoint("akernel.example", 443, "https", False),
            ),
            patch("akernel_sdk.cli._get_auth_token", return_value="token"),
            patch("akernel_sdk.cli._create_ssl_context"),
            patch("akernel_sdk.cli._make_delete_request", side_effect=responses),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.handle_delete(["missing", "sandbox-2"])

        self.assertEqual(raised.exception.code, 1)
        self.assertIn("server returned status 503", stderr.getvalue())

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
