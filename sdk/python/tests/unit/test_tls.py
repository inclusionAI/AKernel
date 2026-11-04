"""TLS configuration contracts and loopback handshakes; no sandbox runtime."""

import asyncio
import contextlib
import io
import json
import os
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import Future
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from adx_environment._command_watch import _CommandWaitManager
from adx_environment._transport import EnvironmentClient
from adx_environment.commands import CommandWaitTimeout

from akernel_sdk import _resource_api, cli
from akernel_sdk._backends import adx, registry


class TLSConfigTest(unittest.TestCase):
    def test_sdk_default_and_explicit_verification_reach_pty(self):
        for setting, expected in (
            (None, False),
            ("0", False),
            ("1", True),
            ("true", True),
        ):
            env = {"AKERNEL_TOKEN": "test-token", "AKERNEL_SERVER_ADDRESS": "localhost"}
            if setting is not None:
                env["AKERNEL_VERIFY_TLS"] = setting
            with self.subTest(setting=setting), patch.dict(os.environ, env, clear=True):
                backend = adx.AdxBackend(registry._config_from_env())
                self.assertEqual(backend._connection.verify_tls, expected)
                pty = backend.pty_for("tls-test")
                self.assertEqual(pty._connection_config.verify_tls, expected)

    def test_invalid_verification_setting_fails_before_backend_loading(self):
        with patch.dict(os.environ, {"AKERNEL_VERIFY_TLS": "tru"}, clear=True):
            with self.assertRaisesRegex(ValueError, "AKERNEL_VERIFY_TLS"):
                cli._create_ssl_context()
            with self.assertRaisesRegex(ValueError, "AKERNEL_VERIFY_TLS"):
                _resource_api._ssl_context()

    def test_cli_flags_configure_sdk_and_inventory_consistently(self):
        for flags, expected in ((["--verify-tls"], "1"), (["--no-verify-tls"], "0")):
            with (
                self.subTest(flags=flags),
                patch.dict(os.environ, {}, clear=True),
                patch("sys.argv", ["ak", *flags, "list"]),
                patch.object(cli, "handle_list") as listing,
            ):
                cli.main()
                self.assertEqual(os.environ["AKERNEL_VERIFY_TLS"], expected)
                listing.assert_called_once()

    def test_cli_ca_file_enables_verification(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("sys.argv", ["ak", "--ca-file", "/example/ca.pem", "resources"]),
            patch.object(cli, "handle_resources") as resources,
        ):
            cli.main()
            self.assertEqual(os.environ["SSL_CERT_FILE"], "/example/ca.pem")
            self.assertEqual(os.environ["AKERNEL_VERIFY_TLS"], "1")
            resources.assert_called_once()

    def test_cli_rejects_ca_file_with_disabled_verification(self):
        with (
            patch("sys.argv", ["ak", "--no-verify-tls", "--ca-file", "ca.pem", "list"]),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()
        self.assertEqual(raised.exception.code, 2)


class _HTTPHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._respond({"items": []})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self._respond({"stdout": "tls-ok", "stderr": "", "exit_code": 0})

    def _respond(self, value):
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class TLSHandshakeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        openssl = shutil.which("openssl")
        if openssl is None:
            raise RuntimeError("openssl is required for TLS handshake tests")
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.root = Path(cls.directory.name)

        def run(*args):
            subprocess.run([openssl, *map(str, args)], check=True, capture_output=True)

        run(
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-keyout",
            cls.root / "ca.key",
            "-out",
            cls.root / "ca.pem",
            "-subj",
            "/CN=AKernel loopback test CA",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
        )
        for name, san in (
            ("valid", "DNS:localhost,IP:127.0.0.1"),
            ("wrong-host", "DNS:wrong.example"),
        ):
            run(
                "req",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                cls.root / f"{name}.key",
                "-out",
                cls.root / f"{name}.csr",
                "-subj",
                "/CN=Loopback test",
            )
            ext = cls.root / f"{name}.ext"
            ext.write_text(
                f"subjectAltName={san}\nextendedKeyUsage=serverAuth\n"
                "basicConstraints=critical,CA:FALSE\n"
                "keyUsage=critical,digitalSignature,keyEncipherment\n"
                "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n"
            )
            run(
                "x509",
                "-req",
                "-in",
                cls.root / f"{name}.csr",
                "-CA",
                cls.root / "ca.pem",
                "-CAkey",
                cls.root / "ca.key",
                "-CAcreateserial",
                "-days",
                "1",
                "-out",
                cls.root / f"{name}.pem",
                "-extfile",
                ext,
            )

    def _server_context(self, name):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.root / f"{name}.pem", self.root / f"{name}.key")
        return context

    def _trust_directory_with_invalid_file(self, kind):
        directory = self.root / "ca-directory"
        directory.mkdir(exist_ok=True)
        subject_hash = subprocess.check_output(
            [
                "openssl",
                "x509",
                "-in",
                str(self.root / "ca.pem"),
                "-subject_hash",
                "-noout",
            ],
            text=True,
        ).strip()
        shutil.copy(self.root / "ca.pem", directory / f"{subject_hash}.0")
        file = self.root / f"{kind}.pem"
        if kind == "malformed":
            file.write_text("not a PEM certificate\n")
        return {"SSL_CERT_FILE": str(file), "SSL_CERT_DIR": str(directory)}

    @contextlib.contextmanager
    def _https(self, name):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _HTTPHandler)
        server.socket = self._server_context(name).wrap_socket(
            server.socket, server_side=True
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_port
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    @contextlib.contextmanager
    def _wss(self, name, *, watch=False, received=None):
        ready = Future()

        async def handler(websocket):
            raw = await websocket.recv()
            if watch:
                message = json.loads(raw)
                received.set()
                self.assertEqual(
                    websocket.request.path, "/api/environment/v1/commands/watch"
                )
                self.assertEqual(message["op"], "subscribe")
                await websocket.send(
                    json.dumps(
                        {
                            "op": "ready",
                            "protocolVersion": 1,
                            "capabilities": ["multiplexed-command-watch"],
                        }
                    )
                )
                await websocket.send(
                    json.dumps({**message["commands"][0], "status": "SUCCEEDED"})
                )
                await websocket.wait_closed()
                return
            await websocket.send(
                json.dumps(
                    {"version": 1, "type": "started", "session_id": "tls-session"}
                )
            )
            await websocket.send(
                json.dumps(
                    {
                        "version": 1,
                        "type": "exited",
                        "session_id": "tls-session",
                        "exit_code": 0,
                    }
                )
            )
            await websocket.wait_closed()

        async def serve():
            from websockets.asyncio.server import serve as ws_serve

            stop = asyncio.Event()
            async with ws_serve(
                handler, "127.0.0.1", 0, ssl=self._server_context(name)
            ) as server:
                ready.set_result(
                    (
                        asyncio.get_running_loop(),
                        stop,
                        server.sockets[0].getsockname()[1],
                    )
                )
                await stop.wait()

        def run():
            try:
                asyncio.run(serve())
            except Exception as error:
                if not ready.done():
                    ready.set_exception(error)
                else:
                    raise

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        loop, stop, port = ready.result(timeout=5)
        try:
            yield port
        finally:
            loop.call_soon_threadsafe(stop.set)
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

    def _env(self, port, *, trusted, verify=True):
        env = {
            "AKERNEL_SERVER_ADDRESS": f"127.0.0.1:{port}",
            "AKERNEL_TOKEN": "loopback-test-token",
        }
        if verify is not None:
            env["AKERNEL_VERIFY_TLS"] = "1" if verify else "0"
        if trusted:
            env["SSL_CERT_FILE"] = str(self.root / "ca.pem")
        return patch.dict(os.environ, env, clear=True)

    def test_trusted_certificate_is_accepted_by_sdk_cli_and_resources(self):
        with self._https("valid") as port, self._env(port, trusted=True):
            backend = adx.AdxBackend(registry._config_from_env())
            client = EnvironmentClient(connection=backend._connection)
            try:
                # Control and direct command/file requests use the dependency pool.
                self.assertEqual(
                    client._http.get(
                        f"https://127.0.0.1:{port}/api/instances"
                    ).status_code,
                    200,
                )
                for action in ("process.exec", "file.read"):
                    self.assertEqual(
                        client.invoke("tls-test", action, {})["exit_code"], 0
                    )
                result = cli._make_get_request(
                    f"https://127.0.0.1:{port}/api/instances",
                    "test",
                    cli._create_ssl_context(),
                )
                self.assertEqual(result["status"], 200)
                self.assertEqual(_resource_api.query_resource_view(), {"items": []})
            finally:
                client.close()

    def test_httpx_rejects_invalid_file_despite_usable_ca_directory(self):
        for kind, error in (
            ("missing", FileNotFoundError),
            ("malformed", ssl.SSLError),
        ):
            trust = self._trust_directory_with_invalid_file(kind)
            with (
                self.subTest(kind=kind),
                self._https("valid") as port,
                self._env(port, trusted=False),
                patch.dict(os.environ, trust),
            ):
                backend = adx.AdxBackend(registry._config_from_env())
                self.assertTrue(backend._connection.verify_tls)
                with self.assertRaises(error):
                    EnvironmentClient(connection=backend._connection)

    def test_openssl_http_uses_directory_despite_invalid_file_and_checks_host(self):
        for kind in ("missing", "malformed"):
            trust = self._trust_directory_with_invalid_file(kind)
            for name in ("valid", "wrong-host"):
                with (
                    self.subTest(kind=kind, name=name),
                    self._https(name) as port,
                    self._env(port, trusted=False),
                    patch.dict(os.environ, trust),
                ):
                    context = cli._create_ssl_context()
                    self.assertTrue(context.check_hostname)
                    self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
                    if name == "valid":
                        with (
                            patch(
                                "sys.argv",
                                ["ak", "--ca-file", trust["SSL_CERT_FILE"], "list"],
                            ),
                            contextlib.redirect_stdout(io.StringIO()),
                        ):
                            cli.main()
                        self.assertEqual(
                            _resource_api.query_resource_view(), {"items": []}
                        )
                    else:
                        with (
                            contextlib.redirect_stderr(io.StringIO()),
                            self.assertRaises(SystemExit),
                        ):
                            cli._make_get_request(
                                f"https://127.0.0.1:{port}/api/instances",
                                "test",
                                context,
                            )
                        with self.assertRaisesRegex(
                            _resource_api.ResourceAPIError, "CERTIFICATE_VERIFY_FAILED"
                        ):
                            _resource_api.query_resource_view()

    def test_openssl_websockets_use_directory_despite_invalid_file(self):
        for kind in ("missing", "malformed"):
            trust = self._trust_directory_with_invalid_file(kind)
            with (
                self.subTest(kind=kind, transport="pty"),
                self._wss("valid") as port,
                self._env(port, trusted=False),
                patch.dict(os.environ, trust),
            ):
                backend = adx.AdxBackend(registry._config_from_env())
                self.assertTrue(backend._connection.verify_tls)
                session = backend.pty_for("tls-test").create("true")
                try:
                    self.assertEqual(session.wait(timeout=5), 0)
                finally:
                    session.close()
            received = threading.Event()
            with (
                self.subTest(kind=kind, transport="command-watch"),
                self._wss("valid", watch=True, received=received) as port,
                self._env(port, trusted=False),
                patch.dict(os.environ, trust),
            ):
                backend = adx.AdxBackend(registry._config_from_env())
                self.assertTrue(backend._connection.verify_tls)
                manager = _CommandWaitManager(backend._connection)
                try:
                    manager.wait("tls-test", "command-test", timeout=3)
                    self.assertTrue(received.is_set())
                finally:
                    thread = manager._thread
                    if thread is not None:
                        thread.join(timeout=7)
                        self.assertFalse(thread.is_alive())

    def test_sdk_cli_and_resources_reject_untrusted_and_wrong_host(self):
        for name, trusted in (("valid", False), ("wrong-host", True)):
            with (
                self.subTest(name=name),
                self._https(name) as port,
                self._env(port, trusted=trusted),
            ):
                backend = adx.AdxBackend(registry._config_from_env())
                client = EnvironmentClient(connection=backend._connection)
                try:
                    with self.assertRaisesRegex(Exception, "CERTIFICATE_VERIFY_FAILED"):
                        client._http.get(f"https://127.0.0.1:{port}/api/instances")
                    with (
                        contextlib.redirect_stderr(io.StringIO()),
                        self.assertRaises(SystemExit),
                    ):
                        cli._make_get_request(
                            f"https://127.0.0.1:{port}/api/instances",
                            "test",
                            cli._create_ssl_context(),
                        )
                    with self.assertRaisesRegex(
                        _resource_api.ResourceAPIError, "CERTIFICATE_VERIFY_FAILED"
                    ):
                        _resource_api.query_resource_view()
                finally:
                    client.close()

    def test_explicit_disabled_verification_accepts_self_signed_endpoint(self):
        with self._https("valid") as port, self._env(port, trusted=False, verify=False):
            self.assertEqual(_resource_api.query_resource_view(), {"items": []})

    def test_default_verification_accepts_self_signed_endpoint(self):
        with self._https("valid") as port, self._env(port, trusted=False, verify=None):
            backend = adx.AdxBackend(registry._config_from_env())
            self.assertFalse(backend._connection.verify_tls)
            client = EnvironmentClient(connection=backend._connection)
            try:
                self.assertEqual(
                    client._http.get(
                        f"https://127.0.0.1:{port}/api/instances"
                    ).status_code,
                    200,
                )
                self.assertEqual(
                    cli._make_get_request(
                        f"https://127.0.0.1:{port}/api/instances",
                        "test",
                        cli._create_ssl_context(),
                    )["status"],
                    200,
                )
                self.assertEqual(_resource_api.query_resource_view(), {"items": []})
            finally:
                client.close()

    def test_pty_tls_rejects_untrusted_and_wrong_host_and_accepts_trusted(self):
        for name, trusted, verify, accepted in (
            ("valid", True, True, True),
            ("valid", False, True, False),
            ("wrong-host", True, True, False),
            ("valid", False, None, True),
        ):
            with (
                self.subTest(name=name, trusted=trusted, verify=verify),
                self._wss(name) as port,
                self._env(port, trusted=trusted, verify=verify),
            ):
                backend = adx.AdxBackend(registry._config_from_env())
                pty = backend.pty_for("tls-test")
                if accepted:
                    session = pty.create("true")
                    try:
                        self.assertEqual(session.wait(timeout=5), 0)
                    finally:
                        session.close()
                else:
                    with self.assertRaisesRegex(Exception, "CERTIFICATE_VERIFY_FAILED"):
                        pty.create("true")

    def test_command_watch_tls_trust_and_verification_policy(self):
        for name, trusted, verify, accepted in (
            ("valid", True, True, True),
            ("valid", False, True, False),
            ("wrong-host", True, True, False),
            ("valid", False, False, True),
            ("valid", False, None, True),
        ):
            received = threading.Event()
            with (
                self.subTest(name=name, trusted=trusted, verify=verify),
                self._wss(name, watch=True, received=received) as port,
                self._env(port, trusted=trusted, verify=verify),
            ):
                backend = adx.AdxBackend(registry._config_from_env())
                manager = _CommandWaitManager(backend._connection)
                try:
                    if accepted:
                        manager.wait("tls-test", "command-test", timeout=3)
                    else:
                        with self.assertRaises(CommandWaitTimeout):
                            manager.wait("tls-test", "command-test", timeout=0.6)
                    self.assertEqual(received.is_set(), accepted)
                finally:
                    thread = manager._thread
                    if thread is not None:
                        thread.join(timeout=7)
                        self.assertFalse(thread.is_alive())
