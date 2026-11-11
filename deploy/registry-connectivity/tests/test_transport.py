"""Exercise the actual seed shell command against local HTTP servers."""
import base64
import http.server
import json
import subprocess
import threading
import unittest

from test_check import check, sha


class TransportTests(unittest.TestCase):
    def test_real_curl_rejects_200_with_truncated_content_length(self):
        body = b"expected-bytes-with-the-correct-content-digest"
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(body) + 10))
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = True
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = 'url = "http://127.0.0.1:%s/probe"\nmax-time = "3"\nnoproxy = "*"\n' % server.server_port
            result = subprocess.run(check.curl_command(len(config.encode())), input=config.encode(), capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0)  # base64 itself succeeds.
            raw = base64.b64decode(result.stdout)
            self.assertIn(body, raw)
            self.assertIn(check.MARKER + b"200", raw)
            self.assertIn(check.EXIT_MARKER + b"18", raw)
            check.digest_bytes(body, sha(body))  # Content digest alone is insufficient.
            with self.assertRaisesRegex(check.CheckFailed, "curl transfer failed"):
                check.decode_response(result.stdout)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_https_connect_response_headers_do_not_become_bootstrap_bytes(self):
        import select
        import socket
        import socketserver
        import ssl
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            cert, key = Path(tmp) / "ca.pem", Path(tmp) / "key.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"], capture_output=True, check=True, timeout=10)
            body = b"actual-bootstrap-content"
            class Registry(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                def log_message(self, *args):
                    pass
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Registry)
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(str(cert), str(key))
            server.socket = tls.wrap_socket(server.socket, server_side=True)
            class Proxy(socketserver.StreamRequestHandler):
                def handle(self):
                    request = self.rfile.readline()
                    if not request.startswith(b"CONNECT "):
                        return
                    while self.rfile.readline().strip():
                        pass
                    with socket.create_connection(("127.0.0.1", server.server_port), timeout=3) as upstream:
                        # Dragonfly can return OK rather than 'Connection established'.
                        self.wfile.write(b"HTTP/1.1 200 OK\r\n\r\n")
                        self.wfile.flush()
                        while True:
                            readable, _, _ = select.select([self.connection, upstream], [], [], 3)
                            if not readable:
                                return
                            for stream in readable:
                                data = stream.recv(65536)
                                if not data:
                                    return
                                (upstream if stream is self.connection else self.connection).sendall(data)
            proxy = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Proxy)
            proxy.daemon_threads = True
            workers = [threading.Thread(target=s.serve_forever, daemon=True) for s in [server, proxy]]
            for worker in workers:
                worker.start()
            try:
                config = f'url = "https://localhost:{server.server_port}/blob"\nproxy = "http://127.0.0.1:{proxy.server_address[1]}"\nnoproxy = ""\nmax-time = "3"\n'
                # No insecure flag: a missing CA must produce a curl failure.
                failed = subprocess.run(check.curl_command(len(config.encode())), input=config.encode(), capture_output=True, timeout=5)
                with self.assertRaisesRegex(check.CheckFailed, "curl transfer failed"):
                    check.decode_response(failed.stdout)
                config += "cacert = " + json.dumps(str(cert)) + "\n"
                result = subprocess.run(check.curl_command(len(config.encode())), input=config.encode(), capture_output=True, timeout=5, check=True)
                status, _, received = check.decode_response(result.stdout)
                self.assertEqual((status, received), (200, body))
            finally:
                for instance in [server, proxy]:
                    instance.shutdown()
                    instance.server_close()
                for worker in workers:
                    worker.join()

    def test_missing_exit_marker_never_counts_as_success(self):
        with self.assertRaises(check.CheckFailed):
            check.decode_response(base64.b64encode(b"HTTP/1.1 200 OK\r\n\r\nbody" + check.MARKER + b"200\n"))

    def test_oversized_compressed_archive_fails_before_tar_parsing(self):
        import gzip
        data = gzip.compress(b"x" * (2 * check.LIMIT + 1))
        descriptor = {"digest": sha(data), "size": len(data)}
        with self.assertRaisesRegex(check.CheckFailed, "decompression limit"):
            check.bootstrap_contents(data, descriptor)

    def test_proxy_logging_and_https_trust_are_required_before_network(self):
        from test_check import configuration
        class HTTP:
            def get(self, *args, **kwargs):
                raise AssertionError("must not send credentials")
        for overrides in [{"proxyLoggingVerified": False}, {"registry": "https://registry.test", "registryCAFile": "/trust/registry.pem"}]:
            with self.subTest(config=overrides), self.assertRaises(check.CheckFailed):
                check.verify_seed(HTTP(), {**configuration(), **overrides}, {})

    def test_oversized_response_is_rejected(self):
        raw = b"HTTP/1.1 200 OK\r\n\r\n" + b"x" * (check.LIMIT + 1)
        raw += check.MARKER + b"200\n" + check.EXIT_MARKER + b"0\n"
        with self.assertRaises(check.CheckFailed):
            check.decode_response(base64.b64encode(raw))

if __name__ == "__main__":
    unittest.main()
