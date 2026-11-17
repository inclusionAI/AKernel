"""Candidate observation must not require enabling destructive operations."""

import hashlib
import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

spec = importlib.util.spec_from_file_location(
    "owned_publication_reclaim",
    Path(__file__).resolve().parents[4] / "builder/managed-docker/reclaim.py",
)
reclaimer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reclaimer)


class ManagedImageReclaim(unittest.TestCase):
    def test_basic_auth_stays_with_the_selected_registry(self):
        raw = json.dumps({"schemaVersion": 2}).encode()
        reference = (
            "registry.test/owned/image@sha256:" + hashlib.sha256(raw).hexdigest()
        )
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, content=raw)

        original = httpx.Client
        with patch.object(
            reclaimer.httpx,
            "Client",
            side_effect=lambda **kw: original(
                transport=httpx.MockTransport(handle), **kw
            ),
        ):
            registry = reclaimer.Registry(
                "https://registry.test", basic_auth=("test", "private-fixture")
            )
        try:
            self.assertEqual(registry.read(reference), {"schemaVersion": 2})
            self.assertTrue(requests[0].headers["Authorization"].startswith("Basic "))
            with self.assertRaisesRegex(ValueError, "outside"):
                registry.read(reference.replace("registry.test", "other.test"))
            self.assertEqual(len(requests), 1)
        finally:
            registry.close()

    def test_authentication_modes_are_mutually_exclusive(self):
        with self.assertRaisesRegex(ValueError, "exactly one"):
            reclaimer.Registry(
                "https://registry.test", "bearer", basic_auth=("test", "fixture")
            )

    def test_disabled_reclamation_still_allows_dry_run_but_never_delete(self):
        reference = "registry.test/publications/owned/service@sha256:" + "a" * 64
        image = MagicMock()
        image.state = "ready"
        image.outputs = {"single": reference}
        image.info = {
            "eligible_for_reclamation": True,
            "reclamation_enabled": False,
            "reclaimable": False,
        }
        registry = MagicMock()
        registry.graph.return_value = [reference]
        result = reclaimer.reclaim(image, registry)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["manifests"], [reference])
        image._mutate.assert_called_once_with("observe")
        with self.assertRaisesRegex(ValueError, "disabled"):
            reclaimer.reclaim(image, registry, apply=True)
        registry.delete.assert_not_called()
