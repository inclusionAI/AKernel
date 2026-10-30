"""Container-accessible tunnel ownership, restore rebinding and byte transport."""

import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from akernel_sdk import ComposeProject, HttpReverseTunnel
from akernel_sdk import _docker_network_guest as guest
from akernel_sdk._backends.errors import BackendOperationError
from akernel_sdk._compose import ServiceHandle
from akernel_sdk.types import CommandResult


class ComposeNetworkTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "source"
        source.mkdir()
        self.original = (
            "services: {worker: {image: test, networks: [private]}}\n"
            "networks: {private: {internal: true}}\n"
        )
        compose = source / "compose.yaml"
        compose.write_text(self.original)
        self.project = ComposeProject.prepare(
            source, [compose], network="bridge", output_dir=root / "out"
        )
        self.sandbox = MagicMock()
        self.project._sandbox = self.sandbox
        self.service = self.project.service("worker")
        self.identity = {"container_id": "full-id", "started_at": "original-start"}
        identity = patch.object(
            ServiceHandle, "identity", return_value=self.identity.copy()
        )
        self.mock_identity = identity.start()
        self.addCleanup(identity.stop)
        self.tunnel = HttpReverseTunnel("http://127.0.0.1:12345")
        self.inspect = {
            "HostConfig": {"NetworkMode": "project_private"},
            "NetworkSettings": {
                "Networks": {
                    "project_private": {
                        "Gateway": "172.28.41.1",
                        "IPAddress": "172.28.41.2",
                    }
                }
            },
        }
        self.sandbox.commands.run.side_effect = [
            CommandResult(stdout=json.dumps([self.inspect]), stderr="", exit_code=0),
            CommandResult(
                stdout='{"url":"http://172.28.41.1:19000"}', stderr="", exit_code=0
            ),
        ]

    def test_native_prepare_has_no_network_overlay(self):
        self.assertEqual(self.project._contents, [self.original])
        self.assertEqual(self.project.descriptor["network"], "bridge")
        self.assertNotIn("127.0.0.1", "".join(self.project._contents))

    def test_gateway_endpoint_is_peer_restricted_and_restore_never_relaunches(self):
        url = self.service.expose_tunnel(self.tunnel)
        self.assertEqual(url, "http://172.28.41.1:19000")
        command = self.sandbox.commands.run.call_args.args[0]
        self.assertIn("172.28.41.2", command)
        descriptor = self.project.descriptor
        self.assertNotIn(self.tunnel.target, json.dumps(descriptor))
        restored = MagicMock()
        attached = ComposeProject.attach(restored, descriptor)
        self.assertEqual(attached.service("worker").expose_tunnel(self.tunnel), url)
        restored.commands.run.assert_not_called()
        attached.reconnect_network()
        self.assertIn("reconnect", restored.commands.run.call_args.args[0])
        self.assertNotIn("dockerd", restored.commands.run.call_args.args[0])
        self.assertEqual(restored.commands.run.call_count, 1)

    def test_missing_default_gateway_uses_internal_bridge_ipam(self):
        self.inspect["NetworkSettings"]["Networks"]["project_private"]["Gateway"] = ""
        self.sandbox.commands.run.side_effect = [
            CommandResult(stdout=json.dumps([self.inspect]), stderr="", exit_code=0),
            CommandResult(
                stdout=json.dumps(
                    [
                        {
                            "IPAM": {
                                "Config": [
                                    {
                                        "Subnet": "172.28.41.0/24",
                                        "Gateway": "172.28.41.1",
                                    }
                                ]
                            }
                        }
                    ]
                ),
                stderr="",
                exit_code=0,
            ),
            CommandResult(
                stdout='{"url":"http://172.28.41.1:19000"}', stderr="", exit_code=0
            ),
        ]
        self.assertEqual(
            self.service.expose_tunnel(self.tunnel), "http://172.28.41.1:19000"
        )

    def test_host_mode_retains_loopback_without_starting_a_proxy(self):
        self.inspect["HostConfig"]["NetworkMode"] = "host"
        self.sandbox.commands.run.side_effect = [
            CommandResult(stdout=json.dumps([self.inspect]), stderr="", exit_code=0)
        ]
        self.assertEqual(self.service.expose_tunnel(self.tunnel), self.tunnel.url)
        self.assertEqual(self.sandbox.commands.run.call_count, 1)
        attached = ComposeProject.attach(MagicMock(), self.project.descriptor)
        self.assertEqual(
            attached.service("worker").expose_tunnel(self.tunnel), self.tunnel.url
        )

    def test_stale_container_and_unknown_launch_are_never_replayed(self):
        self.service.expose_tunnel(self.tunnel)
        self.mock_identity.return_value = {
            "container_id": "different",
            "started_at": "new",
        }
        with self.assertRaisesRegex(BackendOperationError, "replaced"):
            self.service.expose_tunnel(self.tunnel)
        self.assertEqual(self.sandbox.commands.run.call_count, 2)
        self.mock_identity.return_value = self.identity.copy()
        record = self.project._descriptor["tunnel_endpoints"]["worker:8766"]
        record.pop("url")
        with self.assertRaises(BackendOperationError) as failure:
            self.service.expose_tunnel(self.tunnel)
        self.assertEqual(failure.exception.outcome, "unknown")
        self.assertEqual(self.sandbox.commands.run.call_count, 2)

    def test_no_network_or_untrusted_listener_fails_closed(self):
        self.inspect["NetworkSettings"]["Networks"] = {}
        self.sandbox.commands.run.side_effect = [
            CommandResult(stdout=json.dumps([self.inspect]), stderr="", exit_code=0)
        ]
        with self.assertRaisesRegex(ValueError, "IPv4"):
            self.service.expose_tunnel(self.tunnel)
        self.assertEqual(self.sandbox.commands.run.call_count, 1)

    def test_snapshot_attachment_cannot_accidentally_bootstrap_a_missing_relay(self):
        restored = MagicMock()
        attached = ComposeProject.attach(restored, self.project.descriptor)
        with self.assertRaisesRegex(RuntimeError, "existing tunnels"):
            attached.service("worker").expose_tunnel(self.tunnel)
        restored.commands.run.assert_not_called()


class RelayTransportTest(unittest.TestCase):
    def test_http_auth_chunked_sse_and_half_close_are_preserved_byte_for_byte(self):
        payload = (
            b"POST /v1/messages?trace=1 HTTP/1.1\r\n"
            b"Authorization: Bearer opaque\r\nx-api-key: key\r\n\r\n" + b"x" * 200000
        )
        response = (
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n" + b"data: unchanged\n\n" * 10000
        )
        upstream = socket.socket()
        upstream.bind(("127.0.0.1", 0))
        upstream.listen(1)
        received = []

        def server():
            peer, _ = upstream.accept()
            with peer:
                chunks = []
                while chunk := peer.recv(65536):
                    chunks.append(chunk)
                received.append(b"".join(chunks))
                peer.sendall(response)
                peer.shutdown(socket.SHUT_WR)

        thread = threading.Thread(target=server, daemon=True)
        thread.start()
        application, relay = socket.socketpair()
        forwarding = threading.Thread(
            target=guest.forward, args=(relay, upstream.getsockname()), daemon=True
        )
        forwarding.start()
        try:
            application.settimeout(5)
            application.sendall(payload)
            application.shutdown(socket.SHUT_WR)
            result = []
            while chunk := application.recv(65536):
                result.append(chunk)
            self.assertEqual(b"".join(result), response)
            self.assertEqual(received, [payload])
        finally:
            application.close()
            upstream.close()
            forwarding.join(timeout=5)
            thread.join(timeout=5)
        self.assertFalse(forwarding.is_alive())
        self.assertFalse(thread.is_alive())


class EgressRebindTest(unittest.TestCase):
    def test_new_tap_replaces_only_owned_chains_without_flushing_conntrack(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "network.json"
            state.write_text(
                json.dumps(
                    {
                        "uplink": "tap.old",
                        "source": "10.88.0.2",
                        "iptables": "/sbin/iptables-legacy",
                    }
                )
            )
            with (
                patch.object(guest, "STATE", state),
                patch.object(
                    guest.shutil, "which", side_effect=lambda name: "/sbin/" + name
                ),
                patch.object(
                    guest.subprocess,
                    "check_output",
                    return_value="1.1.1.1 via 10.88.0.1 dev tap.new src 10.88.0.3",
                ),
                patch.object(guest.subprocess, "run") as run,
            ):
                record = guest.configure(reconnect=True)
                self.assertEqual(record["source"], "10.88.0.3")
                run.assert_called_once()
                self.assertIn("--noflush", run.call_args.args[0])
                rules = run.call_args.kwargs["input"]
                self.assertIn("AKERNEL_DIND_NAT -o tap.new", rules)
                self.assertNotIn("tap.old", rules)
                self.assertNotIn("10.88.0.2", rules)
                self.assertNotIn("DOCKER", rules)
                self.assertEqual(json.loads(state.read_text()), record)
                run.reset_mock()
                guest.configure(reconnect=True)
                run.assert_not_called()
