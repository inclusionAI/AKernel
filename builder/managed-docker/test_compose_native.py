"""Opt-in official Compose/Buildx plan validation without a daemon or builds."""

import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "runtime_compose_native", Path(__file__).with_name("compose.py")
)
compose = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compose)


def digest(value):
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


class NativeComposePlansTest(unittest.TestCase):
    def setUp(self):
        self.compose = os.environ.get("AKERNEL_TEST_DOCKER_COMPOSE")
        self.buildx = os.environ.get("AKERNEL_TEST_BUILDX")
        if not self.compose or not self.buildx:
            self.skipTest("Set AKERNEL_TEST_DOCKER_COMPOSE and AKERNEL_TEST_BUILDX")
        for binary in [self.compose, self.buildx]:
            self.assertTrue(Path(binary).is_file() and os.access(binary, os.X_OK))

    def check_project(self, services):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            (project / "Dockerfile").write_text("FROM scratch\nCOPY marker /marker\n")
            (project / "marker").write_text("Unchanged native build input\n")
            filename = project / "compose.json"
            filename.write_text(json.dumps({"services": services}))
            original = {p.name: p.read_bytes() for p in project.iterdir()}
            config = root / "docker-config"
            plugins = config / "cli-plugins"
            plugins.mkdir(parents=True)
            (plugins / "docker-buildx").symlink_to(Path(self.buildx).resolve())
            env = {
                k: v for k, v in os.environ.items() if k in ["PATH", "LANG", "LC_ALL"]
            }
            env.update(
                HOME=str(root),
                DOCKER_CONFIG=str(config),
                DOCKER_HOST="unix:///nonexistent-akernel-native-plan-test.sock",
                DOCKER_BUILDKIT="1",
            )

            def execute(args, *, capture=False):
                self.assertEqual(args[:2], ["docker", "compose"])
                return subprocess.check_output(
                    [self.compose, *args[2:]], env=env, text=True, timeout=30
                )

            def print_plan(_args, plan, path, _profile):
                # Docker's parser validates the final native target/group names
                # and Nydus exporter settings. No Solve or push is submitted.
                parsed = json.loads(
                    subprocess.check_output(
                        [
                            self.buildx,
                            "bake",
                            "--file",
                            str(path.with_name("bake.json")),
                            "--print",
                            "default",
                        ],
                        env=env,
                        text=True,
                        timeout=30,
                    )
                )
                active = parsed["group"]["default"]["targets"]
                self.assertEqual(set(active), set(plan["group"]["default"]["targets"]))
                result = {}
                for name in active:
                    target = parsed["target"][name]
                    self.assertEqual(target["output"][0]["compression"], "nydus")
                    identity = target.get("labels", {}).get(
                        "com.docker.compose.service"
                    )
                    result[name] = {
                        "containerimage.digest": digest(
                            identity or target["dockerfile-inline"]
                        )
                    }
                return result

            descriptor = {
                "project_name": "native-plan-test",
                "directory": str(project),
                "files": [str(filename)],
            }
            with (
                patch.dict(os.environ, env, clear=True),
                patch.object(compose, "execute", side_effect=execute),
                patch.object(compose, "bake_metadata", side_effect=print_plan),
            ):
                references = compose.build(
                    descriptor,
                    {
                        "registry": "registry.test",
                        "prefix": "tenant/project",
                        "builder": "remote",
                    },
                )
            self.assertEqual(set(references), set(services))
            self.assertEqual(
                len({ref.split("@")[0] for ref in references.values()}), len(services)
            )
            for service, spec in services.items():
                identity = (
                    service if "build" in spec else "FROM " + spec["image"] + "\n"
                )
                self.assertTrue(references[service].endswith("@" + digest(identity)))
            self.assertEqual(
                original, {p.name: p.read_bytes() for p in project.iterdir()}
            )

    def test_image_only(self):
        self.check_project(
            {"api": {"image": "alpine:3"}, "worker": {"image": "ubuntu:24.04"}}
        )

    def test_dotted_build(self):
        self.check_project({"foo.bar": {"build": "."}})

    def test_dotted_build_with_an_image(self):
        self.check_project({"foo.bar": {"build": ".", "image": "original/unused:tag"}})

    def test_case_distinct_builds(self):
        self.check_project({"Foo": {"build": "."}, "foo": {"build": "."}})

    def test_dotted_build_and_underscore_image(self):
        self.check_project(
            {"foo.bar": {"build": "."}, "foo_bar": {"image": "alpine:3"}}
        )


if __name__ == "__main__":
    unittest.main()
