"""Candidate observation must not require enabling destructive operations."""

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import MagicMock

spec = importlib.util.spec_from_file_location(
    "owned_publication_reclaim",
    Path(__file__).resolve().parents[4] / "builder/managed-docker/reclaim.py",
)
reclaimer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reclaimer)


class ManagedImageReclaim(unittest.TestCase):
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
