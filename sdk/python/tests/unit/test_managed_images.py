"""Managed ownership survives client lifetimes and never silently replays builds."""

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from akernel_sdk import (
    ComposeProject,
    DockerRuntime,
    ImageRetention,
    ManagedImage,
    ManagedImages,
    build_image,
)
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
    def test_environment_connection_stays_bound_to_acquired_handles(self):
        first = Endpoint("cluster-a.test", 443, "https", False)
        second = Endpoint("cluster-b.test", 443, "https", False)
        config_a = BackendConfig(first, first, "fixture-a", False)
        config_b = BackendConfig(second, second, "fixture-b", False)
        captured = []

        def handle(request):
            captured.append(request)
            return httpx.Response(200, json=view())

        original = httpx.Client
        with (
            patch(
                "akernel_sdk._managed_images._config_from_env",
                side_effect=[config_a, config_b, config_b],
            ) as configuration,
            patch(
                "akernel_sdk._managed_images.httpx.Client",
                side_effect=lambda **kw: original(
                    transport=httpx.MockTransport(handle), **kw
                ),
            ),
        ):
            image = ManagedImages().begin("same-logical-request", ImageRetention())
            hold = image.retain("reader")
            hold.release()
        self.assertEqual(configuration.call_count, 1)
        self.assertEqual({request.url.host for request in captured}, {"cluster-a.test"})

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
            connection = MagicMock(images=images)
            with (
                patch.dict(
                    os.environ,
                    {"AKERNEL_SERVER_ADDRESS": "api.test", "AKERNEL_TOKEN": "fixture"},
                    clear=True,
                ),
                patch(
                    "akernel_sdk._client.Client._from_config", return_value=connection
                ),
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
            connection.Sandbox.assert_not_called()
            self.assertEqual(caught.exception.publication_id, IMAGE_ID)

    def test_implicit_build_preserves_environment_gateway_selection(self):
        cases = (
            (
                {
                    "AKERNEL_SERVER_ADDRESS": "api.test",
                    "AKERNEL_GATEWAY_ADDRESS": "https://gateway.test:8443",
                },
                "https://gateway.test:8443",
            ),
            ({"AKERNEL_SERVER_ADDRESS": "api.test"}, "http://api.test"),
            ({"AKERNEL_SERVER_ADDRESS": "api.test:8443"}, "http://api.test:8443"),
            (
                {
                    "AKERNEL_SERVER_ADDRESS": "api.test",
                    "AKERNEL_GATEWAY_ADDRESS": "wss://gateway.test",
                },
                "wss://gateway.test",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "Dockerfile").write_text("FROM scratch\n")
            for environment, gateway in cases:
                with (
                    self.subTest(environment=environment),
                    patch.dict(
                        os.environ,
                        {**environment, "AKERNEL_TOKEN": "fixture"},
                        clear=True,
                    ),
                    patch("akernel_sdk._client.new_backend") as factory,
                    patch(
                        "akernel_sdk._managed_images.ManagedImages.begin",
                        side_effect=BackendOperationError("stop before publishing"),
                    ),
                ):
                    with self.assertRaises(BackendOperationError):
                        build_image(
                            directory, profile="test", retention=ImageRetention()
                        )
                    factory.assert_called_once()
                    config = factory.call_args.args[0]
                    self.assertEqual(config.gateway_endpoint.base_url(), gateway)
                    self.assertEqual(config.token, "fixture")
                    factory.return_value.create.assert_not_called()

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
            connection = MagicMock(images=images)
            connection.Sandbox.return_value = builder
            with (
                patch.dict(
                    os.environ,
                    {
                        "AKERNEL_SERVER_ADDRESS": "api.test",
                        "AKERNEL_GATEWAY_ADDRESS": "https://gateway.test:8443",
                        "AKERNEL_TOKEN": "fixture",
                    },
                    clear=True,
                ),
                patch(
                    "akernel_sdk._client.Client._from_config", return_value=connection
                ) as factory,
            ):
                result = build_image(
                    directory,
                    profile="test",
                    request_id="stable",
                    retention=ImageRetention.ephemeral(),
                )
            self.assertEqual(events, ["plan", "native-publish", "publish"])
            self.assertEqual(result.managed_image.id, IMAGE_ID)
            factory.assert_called_once()
            self.assertEqual(
                factory.call_args.args[0].gateway_endpoint.base_url(),
                "https://gateway.test:8443",
            )
            self.assertEqual(
                connection.Sandbox.call_args.kwargs["extra_config"][
                    "managedImagePublications"
                ],
                [IMAGE_ID],
            )
            builder.kill.assert_called_once()
            self.assertNotIn("release", events)

    def test_compose_rejects_a_sandbox_from_another_connection_before_startup(self):
        first = Endpoint("cluster-a.test", 443, "https", False)
        second = Endpoint("cluster-b.test", 443, "https", False)
        expected = BackendConfig(first, first, "fixture", False)
        wrong = BackendConfig(second, second, "fixture", False)
        with tempfile.TemporaryDirectory() as directory:
            compose = Path(directory) / "compose.yaml"
            compose.write_text("services:\n  app:\n    image: alpine\n")
            images = ManagedImages(expected)
            with patch.object(images, "_request", return_value=view()):
                project = ComposeProject.prepare(
                    directory,
                    [compose],
                    runtime=DockerRuntime(profile="fixture"),
                    image_client=images,
                    retention=ImageRetention(),
                )
            sandbox = type("OtherCluster", (), {"_bound_config": wrong})()
            with patch.object(DockerRuntime, "start") as startup:
                with self.assertRaisesRegex(ValueError, "same Client"):
                    project.start_runtime(sandbox)
            startup.assert_not_called()

    def test_compose_matches_api_identity_even_with_a_different_gateway_route(self):
        endpoint = Endpoint("cluster.test", 443, "https", False)
        gateway = Endpoint("gateway.test", 443, "https", False)
        expected = BackendConfig(endpoint, endpoint, "fixture", False)
        formatted = Endpoint("cluster.test", 443, "https", True)
        equivalent = BackendConfig(formatted, gateway, "fixture", False)
        with tempfile.TemporaryDirectory() as directory:
            compose = Path(directory) / "compose.yaml"
            compose.write_text("services:\n  app:\n    image: alpine\n")
            images = ManagedImages(expected)
            with patch.object(images, "_request", return_value=view()):
                project = ComposeProject.prepare(
                    directory,
                    [compose],
                    runtime=DockerRuntime(profile="fixture"),
                    image_client=images,
                    retention=ImageRetention(),
                )
            sandbox = type("SameCluster", (), {"_bound_config": equivalent})()
            with patch.object(
                DockerRuntime, "start", side_effect=RuntimeError("transport reached")
            ) as startup:
                with self.assertRaisesRegex(RuntimeError, "transport reached"):
                    project.start_runtime(sandbox)
            startup.assert_called_once()

    def test_pagination_rejects_repeated_token(self):
        endpoint = Endpoint("fixture.test", 443, "https", False)
        images = ManagedImages(BackendConfig(endpoint, endpoint, "fixture", False))
        with patch.object(
            images, "_request", return_value={"items": [], "next_page_token": IMAGE_ID}
        ):
            with self.assertRaises(BackendOperationError):
                list(images.list())
