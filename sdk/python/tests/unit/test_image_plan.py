"""OCI metadata/chain integrity and legitimate sharing of a merged root."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from akernel_sdk import DockerRuntime, ImageMountPlan
from akernel_sdk._image_bridge_guest import verify_image_bridge


class ImagePlanTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.chain = "sha256:" + "a" * 64

    def image(self, label, *, architecture="amd64"):
        blobs = []

        def blob(value, media):
            payload = json.dumps(value).encode()
            digest = "sha256:" + hashlib.sha256(payload).hexdigest()
            name = digest.replace(":", "-") + ".json"
            (self.directory / name).write_bytes(payload)
            descriptor = {"digest": digest, "size": len(payload), "mediaType": media}
            blobs.append({"file": name, "descriptor": descriptor})
            return descriptor

        config = blob(
            {
                "architecture": architecture,
                "os": "linux",
                "config": {"Env": ["LABEL=" + label]},
                "rootfs": {"type": "layers", "diff_ids": [self.chain]},
            },
            "application/vnd.oci.image.config.v1+json",
        )
        target = blob(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": config,
                "layers": [
                    {
                        "digest": "sha256:" + "b" * 64,
                        "size": 123,
                        "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                    }
                ],
            },
            "application/vnd.oci.image.manifest.v1+json",
        )
        return {
            "reference": "registry.example/test@" + target["digest"],
            "root": "/opt/akernel-images/" + target["digest"][7:],
            "target": target,
            "config": config,
            "chain_id": self.chain,
            "blobs": blobs,
        }

    def write(self, images, **kwargs):
        path = self.directory / "plan.json"
        path.write_text(
            json.dumps(
                {
                    "images": images,
                    "services": {"main": images[0]["reference"]} if images else {},
                    **kwargs,
                }
            )
        )
        return path

    def test_same_rootfs_with_different_config_digests_is_valid(self):
        first, second = self.image("first"), self.image("second")
        path = self.write([first, second])
        ImageMountPlan.load(path, bases_only=True)

        def run(args, **kwargs):
            if args[0] == "ctr":
                label = "containerd.io/snapshot/agent-service.image-root"
                return SimpleNamespace(
                    stdout=json.dumps(
                        {
                            "Kind": "Committed",
                            "Labels": {
                                label: first["root"]
                            },
                        }
                    )
                )
            image = next(i for i in (first, second) if i["reference"] == args[-1])
            return SimpleNamespace(stdout=image["target"]["digest"])

        with patch("akernel_sdk._image_bridge_guest.subprocess.run", side_effect=run):
            verify_image_bridge(path)

    def test_claimed_chain_must_match_the_selected_manifest_config(self):
        image = self.image("first")
        image["chain_id"] = "sha256:" + "c" * 64
        with self.assertRaises(ValueError):
            ImageMountPlan.load(self.write([image]))

    def test_config_descriptor_must_be_reachable_from_target_manifest(self):
        first, second = self.image("first"), self.image("second")
        first["config"] = second["config"]
        first["blobs"] += second["blobs"]
        with self.assertRaises(ValueError):
            ImageMountPlan.load(self.write([first]))

    def test_foreign_platform_cannot_supply_a_linux_amd64_root(self):
        with self.assertRaises(ValueError):
            ImageMountPlan.load(self.write([self.image("arm", architecture="arm64")]))

    def test_unused_manual_images_do_not_leave_unregistered_aliases(self):
        first, second = self.image("first"), self.image("second")
        aliases = {
            "registry.example/first:tag": first["reference"],
            "registry.example/second:tag": second["reference"],
        }
        plan = ImageMountPlan.load(self.write([first, second], aliases=aliases))
        self.assertEqual(
            plan.aliases, {"registry.example/first:tag": first["reference"]}
        )

    def test_scratch_only_preparation_retains_empty_automatic_plan_marker(self):
        path = self.write([], automatic=True)
        plan = ImageMountPlan.load(path, bases_only=True)
        sandbox = MagicMock()
        sandbox.commands.run.return_value = SimpleNamespace(stdout="[]", stderr="")
        DockerRuntime(images=plan)._bridge(sandbox)
        payload = json.loads(sandbox.files.write.call_args.args[1])
        self.assertIs(payload.get("automatic"), True)
