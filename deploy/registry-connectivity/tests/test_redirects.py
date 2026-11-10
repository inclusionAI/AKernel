"""Registry redirects through the real curl/SeedHTTP path; only exec is local."""
import contextlib
import http.client
import http.server
import json
import subprocess
import threading
from types import SimpleNamespace as NS
from urllib.parse import urlsplit
import unittest
from unittest.mock import patch

from test_check import check, configuration, fixture


@contextlib.contextmanager
def server(handler):
    instance = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield instance
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join()


@contextlib.contextmanager
def local_exec():
    class Client:
        def __init__(self, configuration):
            self.configuration = configuration
        def close(self):
            pass
    class Core:
        def __init__(self, client):
            pass
        def connect_get_namespaced_pod_exec(self):
            pass
    class WS:
        opened, unread = True, True
        def __init__(self, command):
            self.command = command
        def write_stdin(self, config):
            self.result = subprocess.run(self.command, input=config.encode(), capture_output=True, timeout=10)
        def is_open(self):
            return self.opened
        def update(self, **kwargs):
            self.opened = False
        def peek_stdout(self):
            return self.unread
        def read_stdout(self):
            self.unread = False
            return self.result.stdout.decode()
        def peek_stderr(self):
            return False
        def close(self):
            self.opened = False
    modules = {"kubernetes": NS(client=NS(ApiClient=Client, CoreV1Api=Core)), "kubernetes.stream": NS(stream=lambda method, *args, **kwargs: WS(kwargs["command"]))}
    with patch.dict("sys.modules", modules):
        yield check.SeedHTTP(NS(api_client=NS(configuration=None)), "test", "seed", "proxy")


class RedirectTests(unittest.TestCase):
    def verify(self, proxied, cross_origin=False, loop=False, limit=False):
        layer, manifest = fixture()
        events = []
        dummy = "dummy-redirect-pull-token"
        class Objects(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                events.append(("object", self.path, self.headers.get("Authorization")))
                self.send_response(200)
                self.send_header("Content-Length", str(len(layer)))
                self.end_headers()
                self.wfile.write(layer)
            def log_message(self, *args):
                pass
        with server(Objects) as objects:
            class Registry(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    events.append(("registry", self.path, self.headers.get("Authorization")))
                    if self.path == "/v2/":
                        self.send_response(401)
                        self.send_header("WWW-Authenticate", f'Bearer realm="http://127.0.0.1:{self.server.server_port}/auth",service="registry"')
                        self.end_headers()
                        return
                    if self.path.startswith("/auth?"):
                        body = json.dumps({"token": dummy}).encode()
                    elif "/manifests/" in self.path:
                        body = manifest
                    elif self.path.startswith("/objects/") and not limit:
                        body = layer
                    else:
                        target = f"http://127.0.0.1:{objects.server_port}/objects/blob?signature=DUMMY-SIGNED-QUERY" if cross_origin else "/objects/blob"
                        if loop:
                            target = self.path
                        if limit:
                            target = "/objects/" + str(len(events))
                        self.send_response(307)
                        self.send_header("Location", target)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                def log_message(self, *args):
                    pass
            class Proxy(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    target = urlsplit(self.path)
                    with contextlib.closing(http.client.HTTPConnection(target.hostname, target.port, timeout=3)) as upstream:
                        upstream.request("GET", target.path + ("?" + target.query if target.query else ""), headers=dict(self.headers))
                        response = upstream.getresponse()
                        body = response.read()
                        self.send_response(response.status)
                        if response.getheader("Location"):
                            self.send_header("Location", response.getheader("Location"))
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                def log_message(self, *args):
                    pass
            with server(Registry) as registry, server(Proxy) as proxy, local_exec() as seed_http:
                cfg = configuration()
                cfg.update(registry=f"http://127.0.0.1:{registry.server_port}", allowedAuthOrigins=[f"http://127.0.0.1:{registry.server_port}"], seedProxy=f"http://127.0.0.1:{proxy.server_port}", proxyCheckEnabled=proxied)
                if loop or limit:
                    with self.assertRaisesRegex(check.CheckFailed, "redirect (loop|limit)"):
                        check.verify_seed(seed_http, cfg, {"username": "dummy", "password": "dummy"})
                    self.assertLessEqual(len(events), check.MAX_REDIRECTS + 4)
                else:
                    result = check.verify_seed(seed_http, cfg, {"username": "dummy", "password": "dummy"})
                    self.assertTrue(result["passed"])
                    self.assertEqual(result["proxy_checked"], proxied)
                    objects_seen = [e for e in events if e[0] == "object"]
                    if cross_origin:
                        self.assertEqual(len(objects_seen), 2 if proxied else 1)
                        self.assertTrue(all(e[2] is None for e in objects_seen))
                        self.assertTrue(all("signature=DUMMY-SIGNED-QUERY" in e[1] for e in objects_seen))
                    else:
                        completed = [e for e in events if e[1] == "/objects/blob"]
                        self.assertTrue(completed)
                        self.assertTrue(all(e[2] == "Bearer " + dummy for e in completed))

    def test_same_origin_direct_and_proxy_redirects(self):
        for proxy in [False, True]:
            with self.subTest(proxy=proxy):
                self.verify(proxy)

    def test_cross_origin_strips_authorization_direct_and_proxy(self):
        for proxy in [False, True]:
            with self.subTest(proxy=proxy):
                self.verify(proxy, cross_origin=True)

    def test_redirect_loop_and_limit_fail(self):
        self.verify(False, loop=True)
        self.verify(False, limit=True)

    def test_cross_origin_secrets_never_return_and_ca_is_per_origin(self):
        calls = []
        responses = [(307, {"location": "https://objects.test/blob?sig=dummy"}, b""), (302, {"location": "https://registry.test/final"}, b""), (200, {}, b"ok")]
        class HTTP:
            def get(self, url, headers, **kwargs):
                calls.append((url, dict(headers), kwargs.get("ca_file")))
                return responses.pop(0)
        result = check.read_bootstrap(HTTP(), {"registryCAFile": "/trust/registry.pem", "redirectCAFile": "/trust/objects.pem"}, "https://registry.test/blob", {"Authorization": "Bearer dummy", "Cookie": "dummy", "Accept": "blob"})
        self.assertEqual(result, b"ok")
        self.assertEqual([c[2] for c in calls], ["/trust/registry.pem", "/trust/objects.pem", "/trust/registry.pem"])
        self.assertEqual(calls[1][1], {"Accept": "blob"})
        self.assertEqual(calls[2][1], {"Accept": "blob"})

    def test_https_downgrade_and_non_http_redirects_fail_before_next_request(self):
        for location in ["http://objects.test/blob", "file:///etc/passwd", "https://user:password@objects.test/blob"]:
            class HTTP:
                calls = 0
                def get(self, *args, **kwargs):
                    self.calls += 1
                    return 307, {"location": location}, b""
            http = HTTP()
            with self.assertRaises(check.CheckFailed):
                check.read_bootstrap(http, {}, "https://registry.test/blob", {"Authorization": "Bearer dummy"})
            self.assertEqual(http.calls, 1)


if __name__ == "__main__":
    unittest.main()
