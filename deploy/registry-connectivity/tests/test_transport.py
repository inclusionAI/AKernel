"""Exercise the actual seed shell command against local HTTP servers."""
import base64
import http.server
import json
import subprocess
import sys
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

    def test_proxy_logging_ack_is_required_before_any_network_request(self):
        from test_check import configuration
        cfg = configuration()
        cfg["proxyLoggingVerified"] = False
        class HTTP:
            def get(self, *args, **kwargs):
                raise AssertionError("must not send credentials")
        with self.assertRaisesRegex(check.CheckFailed, "credential-safe"):
            check.verify_seed(HTTP(), cfg, {})

    def test_https_proxy_requires_its_own_ca_even_if_registry_ca_exists(self):
        from test_check import configuration
        cfg = configuration()
        cfg.update(registry="https://registry.test", registryCAFile="/trust/registry.pem")
        with self.assertRaisesRegex(check.CheckFailed, "Dragonfly signing CA"):
            check.validate_probe_security(cfg)
        cfg["proxyCAFile"] = "/trust/dragonfly.pem"
        check.validate_probe_security(cfg)

    def test_allowed_cap_and_concurrency_fit_default_memory_limit(self):
        # Fresh process isolates peak RSS from other tests. Exercise worst-case
        # uncompressed layer buffering/decoding at the actual concurrency cap.
        code = '''import base64,concurrent.futures,hashlib,io,resource,sys,tarfile,threading
sys.path.insert(0,sys.argv[1]);from test_check import check,sha
barrier=threading.Barrier(check.CONCURRENCY)
def probe(_):
 out=io.BytesIO()
 with tarfile.open(fileobj=out,mode='w:') as tar:
  member=tarfile.TarInfo('image/image.boot');member.size=check.LIMIT-16384;tar.addfile(member,io.BytesIO(b'x'*member.size))
 layer=out.getvalue();assert len(layer)<=check.LIMIT
 raw=b'HTTP/1.1 200 OK\\r\\n\\r\\n'+layer+check.MARKER+b'200\\n'+check.EXIT_MARKER+b'0\\n'
 encoded=base64.b64encode(raw).decode();chunks=[encoded[i:i+16384] for i in range(0,len(encoded),16384)]
 barrier.wait()
 _,_,body=check.decode_response(''.join(chunks));check.bootstrap_contents(body,{'digest':sha(layer),'size':len(layer)})
with concurrent.futures.ThreadPoolExecutor(max_workers=check.CONCURRENCY) as pool:list(pool.map(probe,range(check.CONCURRENCY)))
peak=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(peak//1024 if sys.platform=='darwin' else peak)
'''
        from pathlib import Path
        result = subprocess.run([sys.executable, "-c", code, str(Path(__file__).parent)], capture_output=True, text=True, timeout=20, check=True)
        peak_kib = int(result.stdout.strip())
        self.assertLess(peak_kib, 192 * 1024)
        self.assertEqual(check.LIMIT, 1024 * 1024)
        self.assertEqual(check.CONCURRENCY, 2)


if __name__ == "__main__":
    unittest.main()
