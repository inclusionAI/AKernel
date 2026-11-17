"""Managed ownership survives client lifetimes and never silently replays builds."""

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from akernel_sdk import ImageRetention, ManagedImage, ManagedImages, build_image
from akernel_sdk._addresses import Endpoint
from akernel_sdk._backends.base import BackendConfig
from akernel_sdk._backends.errors import BackendOperationError
from akernel_sdk.types import CommandResult

IMAGE_ID = "a" * 64
REFERENCE = (
    f"registry.test/managed/publications/{IMAGE_ID}/service-0@sha256:" + "b" * 64
)


def view(state="preparing"):
    return {
        "image": {"id": IMAGE_ID, "state": state, "outputs": {}},
        "reclaimable": False,
    }


class ManagedImagesTest(unittest.TestCase):
    def test_acquisition_handle_releases_its_own_lease(self):
        images = MagicMock()
        images._request.return_value = view()
        image = ManagedImage(images, view())
        hold = image.retain("independent-reader")
        self.assertEqual(hold.image_id, IMAGE_ID)
        hold.release()
        self.assertEqual(
            images._request.call_args.args[2],
            {"operation": "release", "lease_id": "independent-reader"},
        )

    def test_retention_validation_and_default(self):
        self.assertEqual(ImageRetention().to_dict(), {"mode": "retained"})
        self.assertEqual(
            ImageRetention.ephemeral().to_dict(),
            {"mode": "ephemeral", "grace_seconds": 86400},
        )
        for invalid in (True, -1, 31536001, 1.5):
            with self.assertRaises(ValueError):
                ImageRetention.ephemeral(invalid)

    def test_bound_connection_and_http_envelope(self):
        endpoint = Endpoint("example.test", 443, "https", False)
        config = BackendConfig(endpoint, endpoint, "private-test-key", True)
        captured = []

        def handle(request):
            captured.append(request)
            encoded = base64.b64encode(json.dumps(view()).encode()).decode()
            return httpx.Response(200, json={"code": 200, "data": encoded})

        original = httpx.Client
        transport = httpx.MockTransport(handle)
        with patch(
            "akernel_sdk._managed_images.httpx.Client",
            side_effect=lambda **kw: original(transport=transport, **kw),
        ) as factory:
            images = ManagedImages(config)
            image = images.begin("stable-request", ImageRetention.ephemeral())
            image.retain("another-process")
            image.release("another-process")
        self.assertEqual(image.id, IMAGE_ID)
        self.assertTrue(factory.call_args.kwargs["verify"])
        self.assertEqual(captured[0].headers["X-Auth"], "private-test-key")
        self.assertEqual(json.loads(captured[1].content)["lease_id"], "another-process")
        self.assertNotIn("private-test-key", repr(images))

    def test_unknown_publication_is_not_rebuilt(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "Dockerfile").write_text("FROM scratch\n")
            images = MagicMock()
            images.begin.return_value = ManagedImage(images, view("publishing"))
            with (
                patch("akernel_sdk._managed_build.ManagedImages", return_value=images),
                patch("akernel_sdk.sandbox.Sandbox") as factory,
            ):
                with self.assertRaises(BackendOperationError) as caught:
                    build_image(
                        directory,
                        profile="test",
                        request_id="persisted",
                        retention=ImageRetention.ephemeral(),
                    )
            factory.assert_not_called()
            self.assertEqual(caught.exception.publication_id, IMAGE_ID)

    def test_registration_precedes_native_publication_and_cleanup_keeps_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "Dockerfile").write_text("FROM scratch\n")
            events = []
            images = MagicMock()
            publication = ManagedImage(images, view())
            images.begin.return_value = publication

            def mutate(method, identity="", body=None):
                events.append(body["operation"])
                result = view("publishing" if body["operation"] == "plan" else "ready")
                return result

            images._request.side_effect = mutate
            builder = MagicMock()
            builder.id = "builder"

            def run(command, **kwargs):
                if command == "cat /work/publication-plan.json":
                    payload = {
                        "publication_plan": {
                            "single": REFERENCE.split("@")[0] + ":build"
                        }
                    }
                elif command == "cat /work/artifact.json":
                    payload = {"reference": REFERENCE, "image_process": {}}
                else:
                    payload = None
                    if "image-build" in command and "--plan-only" not in command:
                        events.append("native-publish")
                return CommandResult(json.dumps(payload) if payload else "", "", 0)

            builder.commands.run.side_effect = run
            with (
                patch("akernel_sdk._managed_build.ManagedImages", return_value=images),
                patch("akernel_sdk.sandbox.Sandbox", return_value=builder) as factory,
            ):
                result = build_image(
                    directory,
                    profile="test",
                    request_id="stable",
                    retention=ImageRetention.ephemeral(),
                )
            self.assertEqual(events, ["plan", "native-publish", "publish"])
            self.assertEqual(result.managed_image.id, IMAGE_ID)
            self.assertEqual(
                factory.call_args.kwargs["extra_config"]["managedImagePublications"],
                [IMAGE_ID],
            )
            builder.kill.assert_called_once()
            self.assertNotIn("release", events)

    def test_pagination_rejects_repeated_token(self):
        images = ManagedImages()
        with patch.object(
            images, "_request", return_value={"items": [], "next_page_token": IMAGE_ID}
        ):
            with self.assertRaises(BackendOperationError):
                list(images.list())
