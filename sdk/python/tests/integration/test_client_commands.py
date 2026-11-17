"""Live explicit-client isolation, uniform creation and command reconnection."""

import json
import os
import unittest
from unittest.mock import patch

from akernel_sdk import (
    AuthenticationException,
    Client,
    CommandExitException,
)
from tests.integration.test_fork import _ENABLED, _IMAGE, ForkIntegrationFixture


@unittest.skipUnless(_ENABLED and _IMAGE, "enable integration and set a python3 image")
class ClientCommandsIntegrationTest(ForkIntegrationFixture, unittest.TestCase):
    def test_explicit_client_reconnects_command_without_reexecution(self):
        source = self.source
        client = Client(
            api_url=os.environ["AKERNEL_SERVER_ADDRESS"],
            api_key=os.environ["AKERNEL_TOKEN"],
            gateway_address=os.environ["AKERNEL_GATEWAY_ADDRESS"],
            verify_tls=True,
        )
        command = source.commands.run(
            "printf x >> /tmp/execution-count; sleep 2; printf 'reconnected'",
            background=True,
        )
        identity = command.command_id
        self.assertTrue(identity)
        source.close()
        with patch.dict(
            os.environ,
            {
                "AKERNEL_SERVER_ADDRESS": "http://127.0.0.1:1",
                "AKERNEL_TOKEN": "deliberately-invalid",
            },
        ):
            attached = client.Sandbox.connect(source.id)
            self.remember([attached])
            result = attached.commands.connect(identity).wait(timeout=20, check=True)
            self.assertEqual(result.stdout, "reconnected")
            self.assertEqual(attached.files.read("/tmp/execution-count"), "x")
            with self.assertRaises(CommandExitException) as raised:
                attached.commands.run("printf out; printf err >&2; exit 7", check=True)
            self.assertEqual(raised.exception.exit_code, 7)
            self.assertEqual(raised.exception.stdout, "out")
            self.assertEqual(raised.exception.stderr, "err")
            self.assertEqual(attached.commands.run("exit 7").exit_code, 7)
            fresh = client.Sandbox.create(image=_IMAGE, memory=1024, idle_timeout=600)
            self.remember([fresh])
            self.assertEqual(fresh.commands.run("printf fresh").stdout, "fresh")
        bad = Client(
            api_url=os.environ["AKERNEL_SERVER_ADDRESS"],
            api_key="deliberately-invalid",
            verify_tls=True,
        )
        with self.assertRaises(AuthenticationException):
            bad.Sandbox.get_info(source.id)
        self.assertEqual(client.Sandbox.get_info(source.id).state, "running")
        print(
            "proof:",
            json.dumps(
                {
                    "case": "explicit_client_and_command_reconnect",
                    "command_id": identity,
                    "submitted_once": True,
                    "explicit_config_overrides_environment": True,
                    "invalid_client_does_not_poison_valid_client": True,
                    "checked_exit_and_legacy_result": True,
                    "fresh_factory": True,
                }
            ),
            flush=True,
        )
