import base64
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace as NS
import tarfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("registry_check", Path(__file__).resolve().parents[1] / "check.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def sha(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def fixture():
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        data = b"Nydus-bootstrap-probe"
        member = tarfile.TarInfo("image/image.boot")
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))
    layer = output.getvalue()
    manifest = json.dumps({"layers": [{"digest": sha(layer), "size": len(layer), "annotations": {check.BOOTSTRAP: "true"}}]}).encode()
    return layer, manifest


def pod(name, uid=None, is_ready=True):
    return NS(metadata=NS(name=name, uid=uid or name, deletion_timestamp=None), status=NS(conditions=[NS(type="Ready", status="True" if is_ready else "False")], container_statuses=[NS(name="test", image_id="sha256:1", restart_count=0)]))


def configuration():
    return {"namespace": "registry", "deployment": "registry", "registrySelector": {"app": "registry"}, "seedNamespace": "dragonfly", "seedSelector": {"app": "dragonfly", "component": "seed-client"}, "seedContainer": "seed-client", "seedNames": [f"seed-{i}" for i in range(5)], "registry": "http://registry.test:5000", "allowedAuthOrigins": ["http://auth.test:5001"], "repository": "health/probe", "digest": sha(fixture()[1])}


class API:
    def __init__(self):
        self.registry = [pod("registry-0")]
        self.seeds = [pod(f"seed-{i}") for i in range(5)]
        self.generation = 1

    def read_namespaced_deployment(self, *args, **kwargs):
        return NS(metadata=NS(uid="deployment", generation=self.generation), spec=NS(replicas=1), status=NS(observed_generation=self.generation, updated_replicas=1, available_replicas=1, replicas=1))

    def list_namespaced_pod(self, namespace, **kwargs):
        return NS(items=self.registry if namespace == "registry" else self.seeds)


class CheckerTests(unittest.TestCase):
    def test_requires_trusted_token_origin_before_sending_credentials(self):
        with self.assertRaisesRegex(check.CheckFailed, "not explicitly trusted"):
            check.challenge({"www-authenticate": 'Bearer realm="http://other.test/auth",service="registry"'}, ["http://auth.test:5001"])

    def test_response_decodes_binary_and_status(self):
        data = b"\x00\xff\xfe"
        encoded = base64.b64encode(b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n\r\n" + data + check.MARKER + b"200\n")
        status, headers, body = check.decode_response(encoded)
        self.assertEqual((status, body), (200, data))
        self.assertEqual(headers["content-type"], "application/octet-stream")

    def test_missing_curl_status_is_failure(self):
        with self.assertRaises(check.CheckFailed):
            check.decode_response(base64.b64encode(b"curl failed"))

    def test_content_is_verified_and_real_bootstrap_is_read(self):
        layer, manifest = fixture()
        result = check.bootstrap_contents(layer, check.bootstrap_descriptor(json.loads(manifest)))
        self.assertEqual(result["sha256"], hashlib.sha256(b"Nydus-bootstrap-probe").hexdigest())
        with self.assertRaisesRegex(check.CheckFailed, "digest mismatch"):
            check.bootstrap_contents(layer + b"x", check.bootstrap_descriptor(json.loads(manifest)))

    def test_oci_without_nydus_bootstrap_is_rejected(self):
        with self.assertRaises(check.CheckFailed):
            check.bootstrap_descriptor({"layers": [{"size": 1}]})

    def test_authenticated_read_and_dragonfly_proxy_are_both_required(self):
        layer, manifest = fixture()
        calls = []
        class HTTP:
            pod = "seed-0"
            def get(self, url, headers=None, proxy=None):
                calls.append((url, headers, proxy))
                if url.endswith("/v2/"):
                    return 401, {"www-authenticate": 'Bearer realm="http://auth.test:5001/auth",service="registry"'}, b""
                if url.startswith("http://auth.test"):
                    return 200, {}, b'{"token":"secret-token"}'
                if "/manifests/" in url:
                    return 200, {}, manifest
                return 200, {}, layer
        result = check.verify_seed(HTTP(), configuration(), {"username": "pull", "password": "secret-password"})
        self.assertTrue(result["passed"])
        self.assertEqual(len(calls), 5)
        self.assertEqual(calls[-1][2], "http://127.0.0.1:4001")
        self.assertNotIn("secret", json.dumps(result))
        self.assertTrue(calls[1][1]["Authorization"].startswith("Basic "))
        self.assertEqual(calls[2][1]["Authorization"], "Bearer secret-token")

    def test_exec_uses_isolated_api_clients_and_keeps_credentials_off_argv(self):
        clients, commands = [], []
        class Client:
            def __init__(self, configuration):
                self.configuration, self.closed = configuration, False
                clients.append(self)
            def close(self):
                self.closed = True
        class Core:
            def __init__(self, client):
                self.api_client = client
            def connect_get_namespaced_pod_exec(self):
                pass
        class WS:
            opened, unread = True, True
            def write_stdin(self, config):
                self.config = config
            def is_open(self):
                return self.opened
            def update(self, **kwargs):
                self.opened = False
            def peek_stdout(self):
                return self.unread
            def read_stdout(self):
                self.unread = False
                return base64.b64encode(b"HTTP/1.1 200 OK\r\n\r\nbody" + check.MARKER + b"200\n").decode()
            def peek_stderr(self):
                return False
            def close(self):
                self.opened = False
        shared = NS(api_client=NS(configuration=object()))
        def stream(method, *args, **kwargs):
            self.assertIsNot(method.__self__.api_client, shared.api_client)
            commands.append(kwargs["command"])
            return WS()
        modules = {"kubernetes": NS(client=NS(ApiClient=Client, CoreV1Api=Core)), "kubernetes.stream": NS(stream=stream)}
        with patch.dict("sys.modules", modules):
            for name in ["seed-0", "seed-1"]:
                result = check.SeedHTTP(shared, "df", name, "seed").get("http://registry.test/v2/", {"Authorization": "Bearer secret-token"})
                self.assertEqual(result[0], 200)
        self.assertEqual(len(clients), 2)
        self.assertTrue(all(c.closed for c in clients))
        self.assertNotIn("secret-token", repr(commands))

    def test_migration_rollout_and_seed_replacement_change_fingerprint(self):
        api, cfg = API(), configuration()
        first = check.fingerprint(api, api, cfg)
        api.registry[0].metadata.uid = "migrated"
        self.assertNotEqual(check.fingerprint(api, api, cfg), first)
        second = check.fingerprint(api, api, cfg)
        api.generation += 1
        self.assertNotEqual(check.fingerprint(api, api, cfg), second)
        second = check.fingerprint(api, api, cfg)
        api.seeds[0].metadata.uid = "replacement"
        self.assertNotEqual(check.fingerprint(api, api, cfg), second)

    def test_missing_or_unready_seed_is_not_a_success(self):
        api, cfg = API(), configuration()
        api.seeds.pop()
        with self.assertRaises(check.CheckFailed):
            check.fingerprint(api, api, cfg)
        api.seeds.append(pod("seed-4", is_ready=False))
        with self.assertRaises(check.CheckFailed):
            check.fingerprint(api, api, cfg)

    def test_unchanged_state_skips_until_periodic_recheck(self):
        api, cfg = API(), configuration()
        state = {"fingerprint": check.fingerprint(api, api, cfg), "checked_epoch": 100}
        with patch.object(check, "verify_seed", side_effect=AssertionError("unexpected network call")):
            self.assertIsNone(check.execute(api, api, cfg, {}, state, now=101))

    def test_every_seed_must_pass_and_errors_are_redacted(self):
        api, cfg = API(), configuration()
        def verify(http, *args):
            if http.pod == "seed-4":
                raise RuntimeError("secret-password")
            return {"seed": http.pod, "passed": True, "bootstrap": {"sha256": "one"}}
        factory = lambda api, namespace, name, container: NS(pod=name)
        output = io.StringIO()
        with patch.object(check, "verify_seed", side_effect=verify), contextlib.redirect_stdout(output):
            with self.assertRaises(check.CheckFailed):
                check.execute(api, api, cfg, {}, {}, http_factory=factory)
        self.assertNotIn("secret-password", output.getvalue())
        self.assertIn('"passed": false', output.getvalue())

    def test_rollout_during_check_never_advances_success_state(self):
        api, cfg = API(), configuration()
        with patch.object(check, "fingerprint", side_effect=["before", "after"]), patch.object(check, "verify_seed", return_value={"passed": True, "bootstrap": {"sha256": "one"}}):
            with self.assertRaisesRegex(check.CheckFailed, "identity changed"):
                check.execute(api, api, cfg, {}, {})

    def test_five_successes_form_a_new_state(self):
        api, cfg = API(), configuration()
        with patch.object(check, "verify_seed", return_value={"passed": True, "bootstrap": {"sha256": "one"}}):
            state = check.execute(api, api, cfg, {}, {}, now=100)
        self.assertEqual(len(state["seeds"]), 5)
        self.assertEqual(state["fingerprint"], check.fingerprint(api, api, cfg))


if __name__ == "__main__":
    unittest.main()
