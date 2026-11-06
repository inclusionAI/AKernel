"""Keep native Compose service identities through image publication."""

import copy
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "runtime_compose_outputs", Path(__file__).with_name("compose.py")
)
compose = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compose)

FIRST = "sha256:" + "1" * 64
SECOND = "sha256:" + "2" * 64
PROFILE = {"registry": "registry.test", "prefix": "tenant/job", "builder": "remote"}


def native_target(service, **values):
    return {
        "context": "/original/" + service,
        "dockerfile": "Original.Dockerfile",
        "labels": {"com.docker.compose.service": service},
        **values,
    }


class ComposeOutputsTest(unittest.TestCase):
    def run_build(self, services, native, metadata=None):
        observed = {}

        def execute(args, *, capture=False):
            if args[-3:] == ["config", "--format", "json"]:
                return json.dumps({"services": services})
            self.assertEqual(args[-2:], ["build", "--print"])
            return native if isinstance(native, str) else json.dumps(native)

        def solve(_args, plan, _path, _profile):
            observed["plan"] = copy.deepcopy(plan)
            return metadata or {
                name: {"containerimage.digest": FIRST}
                for name in plan["group"]["default"]["targets"]
            }

        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(compose, "execute", side_effect=execute),
            patch.object(compose, "bake_metadata", side_effect=solve) as bake,
        ):
            directory = str(Path(root) / "project")
            descriptor = {
                "directory": directory,
                "project_name": "original",
                "files": [directory + "/compose.yaml"],
            }
            result = compose.build(descriptor, PROFILE)
            observed["bake_calls"] = bake.call_count
            observed["artifacts"] = json.loads(
                Path(directory + ".build/artifacts.json").read_text()
            )
            observed["outputs"] = json.loads(
                Path(directory + ".build/outputs.json").read_text()
            )
        return result, observed

    def test_image_only_project_accepts_successful_empty_native_plan(self):
        for output in ["", " \n"]:
            with self.subTest(output=output):
                result, observed = self.run_build(
                    {"sidecar": {"image": "alpine:3"}}, output
                )
                self.assertTrue(result["sidecar"].endswith("@" + FIRST))
                plan = observed["plan"]
                targets = plan["group"]["default"]["targets"]
                self.assertEqual(len(targets), 1)
                self.assertEqual(
                    plan["target"][targets[0]]["dockerfile-inline"],
                    "FROM alpine:3\n",
                )
                self.assertEqual(observed["bake_calls"], 1)

    def test_dotted_build_service_uses_native_identity_with_or_without_image(self):
        for image in [None, "original/image:tag"]:
            with self.subTest(image=image):
                service = {"build": {"context": "."}}
                if image:
                    service["image"] = image
                original = native_target("foo.bar", args={"KEEP": "original"})
                result, observed = self.run_build(
                    {"foo.bar": service},
                    {"target": {"foo_bar": original}},
                    {"foo_bar": {"containerimage.digest": FIRST}},
                )
                self.assertTrue(result["foo.bar"].endswith("@" + FIRST))
                plan = observed["plan"]
                self.assertEqual(plan["group"]["default"]["targets"], ["foo_bar"])
                for key, value in original.items():
                    self.assertEqual(plan["target"]["foo_bar"][key], value)
                self.assertNotIn("dockerfile-inline", plan["target"]["foo_bar"])

    def test_case_distinct_services_keep_distinct_repositories_and_digests(self):
        result, observed = self.run_build(
            {"Foo": {"build": {}}, "foo": {"build": {}}},
            {"target": {name: native_target(name) for name in ["Foo", "foo"]}},
            {
                "Foo": {"containerimage.digest": FIRST},
                "foo": {"containerimage.digest": SECOND},
            },
        )
        self.assertTrue(result["Foo"].endswith("@" + FIRST))
        self.assertTrue(result["foo"].endswith("@" + SECOND))
        self.assertNotEqual(result["Foo"].split("@")[0], result["foo"].split("@")[0])
        self.assertEqual(observed["artifacts"], result)
        self.assertEqual(observed["outputs"]["Foo"]["target"], "Foo")
        self.assertEqual(observed["outputs"]["foo"]["target"], "foo")
        published = [t["tags"][0] for t in observed["plan"]["target"].values()]
        self.assertEqual(len(set(published)), 2)

    def test_dotted_build_and_underscore_image_service_do_not_share_target(self):
        result, observed = self.run_build(
            {"foo.bar": {"build": {}}, "foo_bar": {"image": "alpine:3"}},
            {"target": {"foo_bar": native_target("foo.bar")}},
        )
        targets = observed["plan"]["group"]["default"]["targets"]
        self.assertEqual(len(set(targets)), 2)
        self.assertEqual(set(result), {"foo.bar", "foo_bar"})
        self.assertNotIn("dockerfile-inline", observed["plan"]["target"]["foo_bar"])

    def test_missing_build_target_never_substitutes_the_service_image(self):
        for output in ["", {"target": {}}]:
            with self.subTest(output=output), self.assertRaises(ValueError):
                self.run_build(
                    {"main": {"build": {}, "image": "stale/image:tag"}}, output
                )

    def test_ambiguous_native_service_labels_are_rejected(self):
        with self.assertRaises(ValueError):
            self.run_build(
                {"main": {"build": {}}},
                {
                    "target": {
                        "first": native_target("main"),
                        "second": native_target("main"),
                    }
                },
            )

    def test_native_build_failure_is_not_interpreted_as_empty_plan(self):
        error = subprocess.CalledProcessError(
            1, ["docker", "compose", "build", "--print"]
        )
        with (
            patch.object(
                compose,
                "execute",
                side_effect=[
                    json.dumps({"services": {"main": {"image": "alpine:3"}}}),
                    error,
                ],
            ),
            patch.object(compose, "bake_metadata") as bake,
            self.assertRaises(subprocess.CalledProcessError) as caught,
        ):
            compose.build(
                {"project_name": "x", "directory": "/x", "files": []}, PROFILE
            )
        self.assertIs(caught.exception, error)
        bake.assert_not_called()

    def test_malformed_nonempty_native_plan_is_not_converted(self):
        with self.assertRaises(json.JSONDecodeError):
            self.run_build({"main": {"image": "alpine:3"}}, "not a Bake plan")

    def test_native_build_context_dependencies_are_preserved(self):
        dependency = native_target("foo.bar")
        worker = native_target("worker", contexts={"base": "target:foo_bar"})
        _, observed = self.run_build(
            {"foo.bar": {"build": {}}, "worker": {"build": {}}},
            {"target": {"foo_bar": dependency, "worker": worker}},
        )
        self.assertEqual(
            observed["plan"]["target"]["worker"]["contexts"],
            {"base": "target:foo_bar"},
        )


if __name__ == "__main__":
    unittest.main()
