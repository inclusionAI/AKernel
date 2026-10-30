#!/usr/bin/env python3
"""Verify the optional runtime reuses an intact, architecture-matched ADX component."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class E2bRuntimeTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "e2b_runtime", ROOT / "builder/scripts/prepare-e2b-runtime.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "component"
        self.source.mkdir()
        self.output = self.root / "output"
        for name in ("adx-execd", "adx-runtime-rootfs.img", "envd-LICENSE"):
            (self.source / name).write_bytes(name.encode())
        self.envd = {"architecture": "x86_64", "version": "0.9.0",
                     "revision": "a" * 40, "sha256": "b" * 64}
        self.write_manifest()

    def write_manifest(self):
        (self.source / "envd-manifest.json").write_text(json.dumps(self.envd))
        files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in self.source.iterdir() if p.name != "manifest.json"}
        (self.source / "manifest.json").write_text(json.dumps({
            "schema_version": 1, "component": "execd", "commit": "c" * 40,
            "target": "x86_64-unknown-linux-gnu", "files": files}))

    def test_reuses_runtime_bytes_and_retains_provenance_and_license(self):
        self.module.prepare(self.source, self.output, "amd64")
        self.assertEqual((self.output / "akernel-runtime-rootfs.img").read_bytes(),
                         (self.source / "adx-runtime-rootfs.img").read_bytes())
        for original, copied in (("manifest.json", "adx-component-manifest.json"),
                                 ("envd-manifest.json", "envd/manifest.json"),
                                 ("envd-LICENSE", "envd/LICENSE")):
            self.assertEqual((self.source / original).read_bytes(),
                             (self.output / "runtime-metadata" / copied).read_bytes())

    def test_release_must_match_commit_target_and_execd_bytes(self):
        spec = importlib.util.spec_from_file_location(
            "verify_e2b_release", ROOT / "builder/scripts/verify-e2b-release.py")
        verifier = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(verifier)
        component = json.loads((self.source / "manifest.json").read_text())
        release = {"commit": component["commit"], "target": component["target"],
                   "files": {"runtime/adx-execd": component["files"]["adx-execd"]}}
        release_path = self.root / "release.json"
        release_path.write_text(json.dumps(release))
        verifier.verify(self.source / "manifest.json", release_path)
        for key in ("commit", "target", "files"):
            changed = {**release, key: {} if key == "files" else "different"}
            release_path.write_text(json.dumps(changed))
            with self.subTest(key=key), self.assertRaises(ValueError):
                verifier.verify(self.source / "manifest.json", release_path)

    def test_rejects_missing_envd_tampering_symlinks_and_wrong_architecture(self):
        cases = ("missing envd", "tampered rootfs", "symlink", "wrong target",
                 "wrong envd architecture", "invalid envd digest")
        for case in cases:
            with self.subTest(case=case):
                self.write_manifest()
                original = (self.source / "adx-runtime-rootfs.img").read_bytes()
                if case == "missing envd":
                    (self.source / "envd-LICENSE").unlink()
                elif case == "tampered rootfs":
                    (self.source / "adx-runtime-rootfs.img").write_bytes(b"changed")
                elif case == "symlink":
                    path = self.source / "envd-LICENSE"
                    path.unlink()
                    path.symlink_to(self.source / "adx-execd")
                else:
                    path = self.source / "manifest.json"
                    manifest = json.loads(path.read_text())
                    if case == "wrong target":
                        manifest["target"] = "aarch64-unknown-linux-gnu"
                        path.write_text(json.dumps(manifest))
                    else:
                        key, value = (("architecture", "aarch64") if case == "wrong envd architecture"
                                      else ("sha256", "invalid"))
                        self.envd[key] = value
                        self.write_manifest()
                with self.assertRaises(ValueError):
                    self.module.prepare(self.source, self.output, "amd64")
                self.assertFalse(self.output.exists())
                (self.source / "adx-runtime-rootfs.img").write_bytes(original)
                license_path = self.source / "envd-LICENSE"
                if license_path.is_symlink():
                    license_path.unlink()
                license_path.write_bytes(b"envd-LICENSE")
                self.envd.update(architecture="x86_64", sha256="b" * 64)


if __name__ == "__main__":
    unittest.main()
