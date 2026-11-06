import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location(
    "runtime_compose", Path(__file__).with_name("compose.py")
)
compose = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compose)


class NativeBuildContract(unittest.TestCase):
    def test_single_build_returns_metadata_without_preparing_helper_rootfs(self):
        with tempfile.TemporaryDirectory() as directory:
            context = str(Path(directory) / "context")
            Path(context).mkdir()
            profile = {
                "registry": "registry.test",
                "prefix": "poc/job",
                "builder": "remote",
                "endpoint": "tcp://builder:9094",
            }
            identity = "sha256:" + "a" * 64
            artifact = {
                "reference": "registry.test/poc/job/service-0@" + identity,
                "image_process": {"WorkingDir": "/app"},
            }

            def bake(args, _plan):
                metadata = args[args.index("--metadata-file") + 1]
                Path(metadata).write_text(
                    json.dumps({"single": {"containerimage.digest": identity}})
                )

            with (
                patch.object(compose, "start_builder"),
                patch.object(compose, "execute_bake", side_effect=bake),
                patch.object(
                    compose, "describe_image", create=True, return_value=artifact
                ) as describe,
                patch.object(
                    compose,
                    "prepare_image",
                    side_effect=AssertionError(
                        "build helper must not pin a task rootfs"
                    ),
                ),
            ):
                self.assertEqual(compose.image_build(context, profile), artifact)
            describe.assert_called_once_with(profile, artifact["reference"])

    def test_task_image_is_exported_without_python_or_path_mutation(self):
        original = {
            "context": "/original",
            "dockerfile": "Custom.Dockerfile",
            "target": "release",
            "args": {"MODE": "native"},
            "secret": ["id=build,src=/secret"],
        }
        plan, refs, _ = compose.build_plan(
            {},
            {"services": {"main": {"build": {}}}},
            {"target": {"main": dict(original)}},
            "registry.test",
            "poc/job",
        )
        self.assertEqual(set(plan["target"]), {"main"})
        self.assertEqual(plan["group"]["default"]["targets"], ["main"])
        actual = plan["target"]["main"]
        for key, value in original.items():
            self.assertEqual(actual[key], value)
        self.assertEqual(actual["tags"], [refs["main"]])
        self.assertNotIn("dockerfile-inline", actual)
        self.assertNotIn("contexts", actual)

    def test_native_target_names_are_not_reserved_by_harness_helpers(self):
        native = {
            "akernel_harness_python": {"context": "/user-helper"},
            "main": {"context": "/main"},
        }
        plan, _, _ = compose.build_plan(
            {},
            {"services": {"main": {"build": {}}}},
            {"target": native},
            "registry.test",
            "poc/job",
            True,
            False,
        )
        self.assertEqual(
            plan["target"]["akernel_harness_python"], {"context": "/user-helper"}
        )
        self.assertTrue(plan["target"]["main"]["no-cache"])
        self.assertEqual(set(plan["target"]), set(native))

    def test_service_discovery_uses_official_compose_and_returns_only_names(self):
        descriptor = {
            "project_name": "test",
            "directory": "/work",
            "files": ["/work/base.yaml", "/work/override.yaml"],
        }
        config = {"services": {"main": {"environment": {"SECRET": "private"}}}}
        with patch.object(
            compose, "execute", return_value=json.dumps(config)
        ) as execute:
            self.assertEqual(compose.describe(descriptor), ["main"])
        self.assertEqual(
            execute.call_args.args[0],
            [
                "docker",
                "compose",
                "-p",
                "test",
                "--project-directory",
                "/work",
                "-f",
                "/work/base.yaml",
                "-f",
                "/work/override.yaml",
                "config",
                "--format",
                "json",
            ],
        )

    def test_native_build_definition_and_cache_inputs_are_preserved(self):
        native = {
            "context": "/work/app",
            "dockerfile": "Dockerfile",
            "target": "release",
            "args": {"MODE": "prod"},
            "secret": ["id=private,src=/run/session"],
            "cache-from": ["type=registry,ref=registry.test/cache:step"],
        }
        plan, refs, _ = compose.build_plan(
            {},
            {"services": {"main": {"build": {}}}},
            {"target": {"main": dict(native)}},
            "registry.test",
            "poc/job",
        )
        actual = plan["target"]["main"]
        for key, value in native.items():
            self.assertEqual(actual[key], value)
        self.assertNotIn("no-cache", actual)
        self.assertNotIn("registry.insecure", actual["output"][0])
        self.assertIn("compression=nydus", actual["output"][0])
        self.assertEqual(refs, {"main": "registry.test/poc/job/service-0:built"})

    def test_image_only_conversion_uses_native_dockerfile_frontend(self):
        image = "registry.test/base@sha256:" + "a" * 64
        plan, _, services = compose.build_plan(
            {},
            {"services": {"loadgen": {"image": image}}},
            {},
            "registry.test",
            "poc/job",
            True,
            True,
        )
        target = plan["target"][services["loadgen"]]
        self.assertEqual(target["dockerfile-inline"], "FROM " + image + "\n")
        self.assertTrue(target["no-cache"])
        self.assertIn("registry.insecure=true", target["output"][0])

    def test_namespace_owner_and_internal_network_use_native_inspection(self):
        relay = MagicMock(return_value={"url": "http://172.28.1.1:23456"})
        values = [
            [{"HostConfig": {"NetworkMode": "container:api-id"}}],
            [
                {
                    "HostConfig": {"NetworkMode": "project-private"},
                    "NetworkSettings": {
                        "Networks": {
                            "private": {"IPAddress": "172.28.1.2", "Gateway": ""}
                        }
                    },
                }
            ],
            [
                {
                    "IPAM": {
                        "Config": [{"Subnet": "172.28.1.0/24", "Gateway": "172.28.1.1"}]
                    }
                }
            ],
        ]
        with (
            patch.object(
                compose, "execute", side_effect=[json.dumps(v) for v in values]
            ) as execute,
            patch.dict(
                sys.modules, {"docker_network_guest": SimpleNamespace(expose=relay)}
            ),
        ):
            self.assertEqual(
                compose.expose_service("main-id", 8766), relay.return_value
            )
        self.assertEqual(
            execute.call_args_list[1].args[0], ["docker", "inspect", "api-id"]
        )
        self.assertEqual(
            relay.call_args.args[1],
            {"address": "172.28.1.1", "allowed": ["172.28.1.2"], "target_port": 8766},
        )

    def test_unsafe_repository_and_image_input_fail_before_build(self):
        for prefix, image in [
            ("poc/../other", "alpine:3"),
            ("poc/job", "ubuntu\nRUN bad"),
        ]:
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                compose.build_plan(
                    {},
                    {"services": {"main": {"image": image}}},
                    {},
                    "registry.test",
                    prefix,
                )


if __name__ == "__main__":
    unittest.main()
