import contextlib
import hashlib
import gzip
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
    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w:") as archive:
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
    return {"namespace": "registry", "deployment": "registry", "registrySelector": {"app": "registry"}, "seedNamespace": "dragonfly", "seedSelector": {"app": "dragonfly", "component": "seed-client"}, "seedContainer": "seed-client", "seedNames": [f"seed-{i}" for i in range(5)], "registry": "http://registry.test:5000", "allowedAuthOrigins": ["http://auth.test:5001"], "proxyLoggingVerified": True, "repository": "health/probe", "digest": sha(fixture()[1])}


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

    def test_content_is_verified_and_real_bootstrap_is_read(self):
        layer, manifest = fixture()
        result = check.bootstrap_contents(layer, check.bootstrap_descriptor(json.loads(manifest)))
        self.assertEqual(result["sha256"], hashlib.sha256(b"Nydus-bootstrap-probe").hexdigest())
        with self.assertRaisesRegex(check.CheckFailed, "digest mismatch"):
            check.bootstrap_contents(layer + b"x", check.bootstrap_descriptor(json.loads(manifest)))

    def test_oci_without_nydus_bootstrap_is_rejected(self):
        with self.assertRaises(check.CheckFailed):
            check.bootstrap_descriptor({"layers": [{"size": 1}]})

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

    def test_configured_three_and_five_seeds_all_pass_and_fail_closed(self):
        for count in [3, 5]:
            api, cfg = API(), configuration()
            api.seeds = api.seeds[:count]
            cfg["seedNames"] = cfg["seedNames"][:count]
            check.fingerprint(api, api, cfg)
            with patch.object(check, "verify_seed", return_value={"passed": True, "bootstrap": {"sha256": "one"}}):
                result = check.execute(api, api, cfg, {}, {})
            self.assertEqual(len(result["seeds"]), count)
            self.assertEqual(result["fingerprint"], check.fingerprint(api, api, cfg))
            api.seeds[-1].status.conditions[0].status = "False"
            with self.assertRaises(check.CheckFailed):
                check.fingerprint(api, api, cfg)
            api.seeds.pop()
            with self.assertRaises(check.CheckFailed):
                check.fingerprint(api, api, cfg)
            api, cfg = API(), configuration()
            cfg["seedNames"] = ["seed-0", "seed-0"]
            with self.assertRaises(check.CheckFailed):
                check.fingerprint(api, api, cfg)
            cfg["seedNames"] = []
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

if __name__ == "__main__":
    unittest.main()
