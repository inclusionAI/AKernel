"""Planning may not publish or depend on a disposable builder for ownership."""

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "owned_publication_compose", Path(__file__).with_name("compose.py")
)
compose = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compose)


class ManagedPublicationPlans(unittest.TestCase):
    def test_single_plan_is_stable_and_does_not_start_buildkit(self):
        profile = {"registry": "registry.test", "prefix": "managed/session"}
        with (
            patch.object(compose, "start_builder") as start,
            patch.object(compose, "bake_metadata") as bake,
        ):
            first = compose.image_build(
                "/context", profile, publication_id="a" * 64, plan_only=True
            )
            second = compose.image_build(
                "/context", profile, publication_id="a" * 64, plan_only=True
            )
        self.assertEqual(first, second)
        self.assertIn(
            "/publications/" + "a" * 64 + "/service-0",
            first["publication_plan"]["single"],
        )
        start.assert_not_called()
        bake.assert_not_called()

    def test_invalid_publication_cannot_escape_its_prefix(self):
        with self.assertRaises(ValueError):
            compose.publication_prefix({"prefix": "managed"}, "../history")
