# Copyright (c) 2026 Ant Group Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import inspect
import os
import unittest
from unittest.mock import MagicMock, patch

from akernel_sdk import (
    CheckpointInfo,
    DockerfileLaunch,
    HttpReverseTunnel,
    NetworkPolicy,
    S3Config,
    Sandbox,
)
from akernel_sdk import sandbox as sandbox_module
from akernel_sdk._dockercontext import LocalDockerContext
from akernel_sdk._dockerfile import DockerfileBuildError, DockerfileParseError
from akernel_sdk._dockerfile_runner import DockerfileApplyResult
from akernel_sdk.types import SandboxInfo


class SandboxTest(unittest.TestCase):
    def setUp(self):
        self.session = MagicMock()
        self.session.id = "physical-id"
        self.session.commands = MagicMock()
        self.session.files = MagicMock()
        self.session.is_running.return_value = True
        self.session.get_info.return_value = SandboxInfo(
            id="physical-id",
            state="running",
            cpu=2000,
            memory=8192,
            image=None,
        )
        self.backend = MagicMock()
        self.backend.create.return_value = self.session
        self.load_backend = patch.object(
            sandbox_module,
            "load_backend",
            return_value=self.backend,
        )
        self.load_backend.start()
        self.addCleanup(self.load_backend.stop)

    def test_default_constructor_and_info(self):
        sandbox = Sandbox(cpu=2000, memory=8192)
        self.assertEqual(sandbox.id, "physical-id")
        self.assertIsNone(sandbox.reverse_tunnel)
        self.assertTrue(sandbox.is_running())
        self.assertEqual(sandbox.get_info().id, "physical-id")
        self.assertEqual(sandbox.get_info().cpu, 2000)
        self.assertIsNone(sandbox.get_info().xpu)
        self.assertIsNone(sandbox.get_info().storage_mb)
        self.assertIsNone(sandbox.startup_command)

        spec = self.backend.create.call_args.args[0]
        self.assertEqual(spec.cpu, 2000)
        self.assertEqual(spec.memory, 8192)
        self.assertEqual(dict(spec.env), {})
        self.assertIsNone(spec.xpu)
        self.assertIsNone(spec.storage_mb)
        self.assertIsNone(spec.network_policy)
        self.assertFalse(spec.failover)
        self.assertEqual(dict(spec.extra_config), {})
        sandbox.kill()
        self.session.terminate.assert_called_once_with()
        self.session.close.assert_called_once_with()

    def test_failover_is_typed_and_forwarded(self):
        sandbox = Sandbox(failover=True)
        spec = self.backend.create.call_args.args[0]

        self.assertTrue(spec.failover)
        sandbox.kill()

    def test_failover_rejects_non_boolean_values(self):
        with self.assertRaisesRegex(TypeError, "failover"):
            Sandbox(failover=1)
        self.backend.create.assert_not_called()

    def test_reload_cold_start_success_returns_true_without_replacing_facades(self):
        self.session.reload.return_value = True
        sandbox = Sandbox()
        before = (sandbox.commands, sandbox.files, sandbox.pty, sandbox._session)

        self.assertIs(sandbox.reload(), True)
        self.assertEqual(
            (sandbox.commands, sandbox.files, sandbox.pty, sandbox._session),
            before,
        )
        self.session.reload.assert_called_once_with()

    def test_reload_returns_false_after_close(self):
        sandbox = Sandbox()
        sandbox.kill()

        self.assertIs(sandbox.reload(), False)
        self.session.reload.assert_not_called()

    def test_extra_config_is_validated_and_defensively_copied(self):
        labels = ["worker"]
        requested = {"featureFlag": True, "nested": {"labels": labels}}

        sandbox = Sandbox(extra_config=requested)
        spec = self.backend.create.call_args.args[0]
        requested["featureFlag"] = False
        labels.append("mutated")

        self.assertEqual(
            dict(spec.extra_config),
            {"featureFlag": True, "nested": {"labels": ["worker"]}},
        )
        sandbox.kill()

    def test_extra_config_rejects_non_json_values(self):
        invalid = (
            (["not", "a", "mapping"], TypeError),
            ({1: "non-string key"}, TypeError),
            ({"value": object()}, TypeError),
            ({"value": float("inf")}, ValueError),
        )
        circular: dict[str, object] = {}
        circular["self"] = circular
        invalid += ((circular, ValueError),)

        for value, error_type in invalid:
            with self.subTest(value=value), self.assertRaises(error_type):
                Sandbox(extra_config=value)
        self.backend.create.assert_not_called()

    def test_kill_is_idempotent(self):
        sandbox = Sandbox()
        sandbox.kill()
        sandbox.kill()
        self.session.terminate.assert_called_once_with()
        self.session.close.assert_called_once_with()

    def test_detached_sandbox_is_not_terminated_by_kill(self):
        sandbox = Sandbox(name="worker", detached=True)
        sandbox.kill()
        self.session.terminate.assert_not_called()
        self.session.close.assert_called_once_with()

    def test_termination_failure_still_closes_local_resources(self):
        remote_error = RuntimeError("remote delete failed")
        self.session.terminate.side_effect = [remote_error, None]
        sandbox = Sandbox()
        pty = MagicMock()
        sandbox._pty = pty

        with self.assertRaisesRegex(RuntimeError, "remote delete failed") as raised:
            sandbox.kill()

        self.assertIs(raised.exception, remote_error)
        pty._close.assert_called_once_with()
        self.session.close.assert_called_once_with()

        sandbox.kill()
        sandbox.kill()

        self.assertEqual(self.session.terminate.call_count, 2)
        pty._close.assert_called_once_with()
        self.session.close.assert_called_once_with()

    def test_termination_error_takes_precedence_over_local_cleanup_error(self):
        remote_error = RuntimeError("remote delete failed")
        self.session.terminate.side_effect = remote_error
        self.session.close.side_effect = RuntimeError("client close failed")
        sandbox = Sandbox()
        pty = MagicMock()
        pty._close.side_effect = RuntimeError("PTY close failed")
        sandbox._pty = pty

        with (
            self.assertLogs(sandbox_module.logger, level="WARNING"),
            self.assertRaisesRegex(
                RuntimeError,
                "remote delete failed",
            ) as raised,
        ):
            sandbox.kill()

        self.assertIs(raised.exception, remote_error)
        pty._close.assert_called_once_with()
        self.session.close.assert_called_once_with()

    def test_named_delete_hides_backend_namespace(self):
        Sandbox.delete("worker")
        self.backend.delete_named.assert_called_once_with("worker")

    def test_checkpoint_returns_public_identity_and_keeps_source_running(self):
        self.session.checkpoint.return_value = "checkpoint-1"
        sandbox = Sandbox()

        checkpoint = sandbox.checkpoint(timeout=240)

        self.assertEqual(checkpoint, CheckpointInfo("checkpoint-1"))
        self.session.checkpoint.assert_called_once_with(timeout=240)
        self.session.terminate.assert_not_called()
        sandbox.kill()

    def test_checkpoint_can_terminate_source_after_success(self):
        self.session.checkpoint.return_value = "checkpoint-1"
        sandbox = Sandbox()

        checkpoint = sandbox.checkpoint(leave_running=False)

        self.assertEqual(checkpoint.id, "checkpoint-1")
        self.session.terminate.assert_called_once_with()
        self.session.close.assert_called_once_with()

    def test_checkpoint_validates_arguments_and_running_state(self):
        sandbox = Sandbox()
        for timeout in (True, 0, -1, 1.5):
            with self.subTest(timeout=timeout), self.assertRaises(
                (TypeError, ValueError)
            ):
                sandbox.checkpoint(timeout=timeout)
        with self.assertRaisesRegex(TypeError, "leave_running"):
            sandbox.checkpoint(leave_running=1)
        self.session.is_running.return_value = False
        with self.assertRaisesRegex(RuntimeError, "running sandbox"):
            sandbox.checkpoint()
        sandbox.kill()

    def test_restore_builds_facades_around_new_backend_session(self):
        restored_session = MagicMock()
        restored_session.id = "restored-physical-id"
        restored_session.commands = MagicMock()
        restored_session.files = MagicMock()
        restored_session.get_info.return_value = SandboxInfo(
            id="restored-physical-id",
            state="running",
            cpu=2000,
            memory=8192,
            image="base-image",
        )
        self.backend.restore.return_value = restored_session
        tunnel = HttpReverseTunnel("http://127.0.0.1:9000")

        restored = Sandbox.restore(
            CheckpointInfo("checkpoint-1"), reverse_tunnel=tunnel
        )

        self.backend.restore.assert_called_once_with(
            "checkpoint-1", reverse_tunnel=tunnel
        )
        self.assertEqual(restored.id, "restored-physical-id")
        self.assertIs(restored.reverse_tunnel, tunnel)
        self.assertEqual(restored.get_info().cpu, 2000)
        restored.kill()
        restored_session.terminate.assert_called_once_with()
        restored_session.close.assert_called_once_with()

    def test_list_and_delete_checkpoints_hide_backend_details(self):
        self.backend.list_checkpoints.return_value = ["checkpoint-1", "checkpoint-2"]

        self.assertEqual(
            Sandbox.list_checkpoints(),
            [CheckpointInfo("checkpoint-1"), CheckpointInfo("checkpoint-2")],
        )
        Sandbox.delete_checkpoint(CheckpointInfo("checkpoint-1"))
        Sandbox.delete_checkpoint(" checkpoint-2 ")

        self.assertEqual(
            self.backend.delete_checkpoint.call_args_list,
            [unittest.mock.call("checkpoint-1"), unittest.mock.call("checkpoint-2")],
        )
        with self.assertRaises(ValueError):
            Sandbox.delete_checkpoint(" ")
        with self.assertRaises(TypeError):
            Sandbox.restore(object())  # type: ignore[arg-type]

    def test_rootfs_requires_s3_config(self):
        with self.assertRaisesRegex(TypeError, "S3Config"):
            Sandbox(rootfs={"type": "s3"})
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            Sandbox(
                image="ubuntu:24.04",
                rootfs=S3Config("https://s3.example.com", "rootfs", "rootfs.img"),
            )
        self.backend.create.assert_not_called()

    def test_runtime_identifier_is_normalized_and_passed_to_backend(self):
        sandbox = Sandbox(runtime=" gvisor-next ")
        spec = self.backend.create.call_args.args[0]
        self.assertEqual(spec.runtime, "gvisor-next")
        sandbox.kill()

    def test_runtime_identifier_validation(self):
        for value in (None, 1):
            with self.subTest(value=value), self.assertRaisesRegex(
                TypeError, "runtime must be a string"
            ):
                Sandbox(runtime=value)
        for value in ("", "   "):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "runtime must be a non-empty string"
            ):
                Sandbox(runtime=value)
        self.backend.create.assert_not_called()

    def test_xpu_request_is_normalized_and_delegated_to_backend(self):
        sandbox = Sandbox(runtime="gpu-runtime", xpu=" GPU:L20:02 ")
        self.assertEqual(sandbox.get_info().xpu, "gpu:l20:2")
        spec = self.backend.create.call_args.args[0]
        self.assertEqual(spec.runtime, "gpu-runtime")
        self.assertEqual(spec.xpu, "gpu:l20:2")
        sandbox.kill()

    def test_xpu_request_validation(self):
        invalid = (
            (1, TypeError),
            ("gpu", ValueError),
            ("gpu::1", ValueError),
            ("npu:l20:1", ValueError),
            ("gpu:l20:0", ValueError),
            ("gpu:l20:1.5", ValueError),
            ("gpu:l20/evil:1", ValueError),
        )
        for value, error_type in invalid:
            with self.subTest(value=value), self.assertRaises(error_type):
                Sandbox(xpu=value)
        self.backend.create.assert_not_called()

    def test_storage_request_is_delegated_to_backend(self):
        sandbox = Sandbox(runtime="storage-runtime", storage_mb=256)
        self.assertEqual(sandbox.get_info().storage_mb, 256)
        spec = self.backend.create.call_args.args[0]
        self.assertEqual(spec.runtime, "storage-runtime")
        self.assertEqual(spec.storage_mb, 256)
        sandbox.kill()

    def test_storage_request_validation(self):
        for value in (True, 0, -1, 1.5):
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                Sandbox(storage_mb=value)
        self.backend.create.assert_not_called()

    def test_block_network_policy_is_passed_to_backend(self):
        policy = NetworkPolicy.block()

        sandbox = Sandbox(network_policy=policy)

        spec = self.backend.create.call_args.args[0]
        self.assertIs(spec.network_policy, policy)
        self.assertEqual(policy.to_dict(), {"blockNetwork": True})
        sandbox.kill()

    def test_dns_blacklist_is_normalized_and_passed_to_backend(self):
        policy = NetworkPolicy.deny_dns("GitHub.COM.", "*.GitHub.com", "github.com")

        sandbox = Sandbox(network_policy=policy)

        spec = self.backend.create.call_args.args[0]
        self.assertEqual(
            spec.network_policy.to_dict(),
            {"dnsBlacklist": ["github.com", "*.github.com"]},
        )
        sandbox.kill()

    def test_empty_network_policy_is_treated_as_unrestricted(self):
        sandbox = Sandbox(network_policy=NetworkPolicy())

        spec = self.backend.create.call_args.args[0]
        self.assertIsNone(spec.network_policy)
        sandbox.kill()

    def test_invalid_network_policy_is_rejected_before_backend(self):
        invalid_factories = (
            lambda: NetworkPolicy(block_network="yes"),
            lambda: NetworkPolicy(dns_blacklist="github.com"),
            lambda: NetworkPolicy.deny_dns(),
            lambda: NetworkPolicy.deny_dns("github.*"),
            lambda: NetworkPolicy(block_network=True, dns_blacklist=("github.com",)),
        )
        for factory in invalid_factories:
            with (
                self.subTest(factory=factory),
                self.assertRaises((TypeError, ValueError)),
            ):
                factory()
        with self.assertRaisesRegex(TypeError, "NetworkPolicy"):
            Sandbox(network_policy={"blockNetwork": True})
        self.backend.create.assert_not_called()

    def test_cwd_must_be_absolute(self):
        with self.assertRaisesRegex(ValueError, "absolute POSIX"):
            Sandbox(cwd="workspace")

    def test_common_resource_validation_happens_before_backend(self):
        with self.assertRaisesRegex(ValueError, "cpu_limit"):
            Sandbox(cpu=2000, cpu_limit=1000)
        for value in (0, -1, -2):
            with self.subTest(schedule_timeout=value), self.assertRaisesRegex(
                ValueError, "schedule_timeout"
            ):
                Sandbox(schedule_timeout=value)
        self.backend.create.assert_not_called()

    def test_port_forwardings_are_integer_ports(self):
        with self.assertRaisesRegex(TypeError, "integer"):
            Sandbox(port_forwardings=["8080"])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            Sandbox(port_forwardings=[8080, 8080])

    def test_reverse_tunnel_is_passed_through_spec(self):
        tunnel = HttpReverseTunnel(
            "https://example.com",
            reverse_port=9000,
            listen_port=9001,
        )
        sandbox = Sandbox(reverse_tunnel=tunnel)
        self.assertIs(sandbox.reverse_tunnel, tunnel)
        self.assertEqual(sandbox.reverse_tunnel.url, "http://127.0.0.1:9001")
        spec = self.backend.create.call_args.args[0]
        self.assertIs(spec.reverse_tunnel, tunnel)
        sandbox.kill()
        self.session.close.assert_called_once_with()

    def test_reverse_tunnel_port_conflict(self):
        tunnel = HttpReverseTunnel(
            "http://127.0.0.1:8000",
            reverse_port=9000,
            listen_port=9001,
        )
        with self.assertRaisesRegex(ValueError, "conflict"):
            Sandbox(port_forwardings=[9000], reverse_tunnel=tunnel)
        with self.assertRaisesRegex(ValueError, "conflict"):
            Sandbox(port_forwardings=[9001], reverse_tunnel=tunnel)

    def test_backend_create_failure_is_reported(self):
        self.backend.create.side_effect = RuntimeError("timeout")
        with self.assertRaisesRegex(RuntimeError, "timeout"):
            Sandbox(reverse_tunnel=HttpReverseTunnel("example.com"), detached=True)

    def test_partial_facade_initialization_rolls_back_remote_sandbox(self):
        with (
            patch.object(sandbox_module, "Pty", side_effect=RuntimeError("pty")),
            self.assertRaisesRegex(RuntimeError, "pty"),
        ):
            Sandbox(detached=True)
        self.session.terminate.assert_called_once_with()
        self.session.close.assert_called_once_with()

    def test_partial_facade_cleanup_preserves_initialization_error(self):
        self.session.terminate.side_effect = RuntimeError("remote delete failed")
        self.session.close.side_effect = RuntimeError("client close failed")
        initialization_error = RuntimeError("PTY initialization failed")
        with (
            patch.object(
                sandbox_module,
                "Pty",
                side_effect=initialization_error,
            ),
            self.assertLogs(sandbox_module.logger, level="WARNING"),
            self.assertRaisesRegex(
                RuntimeError,
                "PTY initialization failed",
            ) as raised,
        ):
            Sandbox(name="worker", detached=True)

        self.assertIs(raised.exception, initialization_error)
        self.session.terminate.assert_called_once_with()
        self.session.close.assert_called_once_with()

    def test_dockerfile_signature_and_mutual_exclusion(self):
        parameters = inspect.signature(Sandbox).parameters
        self.assertIn("dockerfile", parameters)
        self.assertNotIn("context", parameters)
        self.assertNotIn("auto_start_cmd", parameters)
        self.assertNotIn("build_run_timeout", parameters)

        dockerfile = DockerfileLaunch(LocalDockerContext("FROM ubuntu\n"))
        for kwargs in (
            {"image": "ubuntu", "dockerfile": dockerfile},
            {
                "rootfs": S3Config("https://s3.example.com", "bucket", "rootfs"),
                "dockerfile": dockerfile,
            },
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(ValueError, "mutually exclusive"):
                    Sandbox(**kwargs)
        with self.assertRaisesRegex(TypeError, "dockerfile"):
            Sandbox(dockerfile=object())  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            Sandbox(context=LocalDockerContext("FROM ubuntu\n"))  # type: ignore[call-arg]
        self.backend.create.assert_not_called()

    def test_dockerfile_context_is_strict_before_backend_creation(self):
        unsupported = (
            "ADD https://example.test/a /opt/",
            'COPY ["a b", "/dest/"]',
            'ADD ["a b", "/dest/"]',
            'RUN ["printf", "%s", "x"]',
            "WORKDIR app",
            "ARG VERSION=1",
            "COPY --chmod=755 a /dest/",
            "COPY --link a /dest/",
            "COPY --parents a /dest/",
            "FROM --platform=linux/amd64 ubuntu",
        )
        for instruction in unsupported:
            with self.subTest(instruction=instruction):
                dockerfile = (
                    instruction
                    if instruction.startswith("FROM")
                    else f"FROM ubuntu\n{instruction}\n"
                )
                with self.assertRaises(DockerfileParseError):
                    Sandbox(dockerfile=DockerfileLaunch(LocalDockerContext(dockerfile)))
        self.backend.create.assert_not_called()

    def test_dockerfile_context_uses_base_image_and_applies_after_facades(self):
        context = LocalDockerContext("FROM ubuntu:24.04\nRUN true\n")
        dockerfile = DockerfileLaunch(
            context,
            auto_start_cmd=False,
            run_timeout=300,
        )
        apply_result = DockerfileApplyResult(
            start_cmd=None,
            startup_command=None,
            entrypoint=None,
            warnings=(),
        )
        with patch(
            "akernel_sdk._dockerfile_runner.apply_dockerfile",
            return_value=apply_result,
        ) as apply:
            sandbox = Sandbox(dockerfile=dockerfile)

        spec = self.backend.create.call_args.args[0]
        self.assertEqual(spec.image, "ubuntu:24.04")
        self.assertIs(apply.call_args.args[0], sandbox)
        self.assertIsNotNone(sandbox._files)
        self.assertIsNotNone(sandbox._commands)
        self.assertIsNotNone(sandbox._pty)
        self.assertIs(apply.call_args.args[2], context)
        self.assertFalse(apply.call_args.kwargs["auto_start_cmd"])
        self.assertEqual(apply.call_args.kwargs["run_timeout"], 300)
        self.assertIsNone(sandbox.startup_command)
        sandbox.kill()

    def test_dockerfile_context_exposes_startup_command_handle(self):
        context = LocalDockerContext('FROM ubuntu:24.04\nCMD ["server"]\n')
        startup_handle = object()
        apply_result = DockerfileApplyResult(
            start_cmd=("server",),
            startup_command=startup_handle,
            entrypoint=None,
            warnings=(),
        )
        with patch(
            "akernel_sdk._dockerfile_runner.apply_dockerfile",
            return_value=apply_result,
        ):
            sandbox = Sandbox(dockerfile=DockerfileLaunch(context))

        self.assertIs(sandbox.startup_command, startup_handle)
        sandbox.kill()

    def test_dockerfile_context_without_dispatched_command_has_no_startup_handle(self):
        context = LocalDockerContext("FROM ubuntu:24.04\n")
        for auto_start_cmd in (False, True):
            with self.subTest(auto_start_cmd=auto_start_cmd):
                apply_result = DockerfileApplyResult(
                    start_cmd=None,
                    startup_command=None,
                    entrypoint=None,
                    warnings=(),
                )
                with patch(
                    "akernel_sdk._dockerfile_runner.apply_dockerfile",
                    return_value=apply_result,
                ):
                    sandbox = Sandbox(
                        dockerfile=DockerfileLaunch(
                            context,
                            auto_start_cmd=auto_start_cmd,
                        )
                    )
                self.assertIsNone(sandbox.startup_command)
                sandbox.kill()

    def test_dockerfile_startup_dispatch_failure_terminates_and_closes_session(self):
        startup_error = DockerfileBuildError(
            "Failed to dispatch startup command",
            instruction="CMD",
        )
        with patch(
            "akernel_sdk._dockerfile_runner.apply_dockerfile",
            side_effect=startup_error,
        ):
            with self.assertRaises(DockerfileBuildError) as raised:
                Sandbox(
                    dockerfile=DockerfileLaunch(
                        LocalDockerContext('FROM ubuntu\nCMD ["server"]\n')
                    ),
                    detached=True,
                )

        self.assertIs(raised.exception, startup_error)
        self.session.terminate.assert_called_once_with()
        self.session.close.assert_called_once_with()

    def test_dockerfile_build_failure_terminates_and_closes_detached_session(self):
        build_error = DockerfileBuildError("RUN failed")
        with patch(
            "akernel_sdk._dockerfile_runner.apply_dockerfile",
            side_effect=build_error,
        ):
            with self.assertRaises(DockerfileBuildError) as raised:
                Sandbox(
                    dockerfile=DockerfileLaunch(
                        LocalDockerContext("FROM ubuntu\nRUN false\n")
                    ),
                    detached=True,
                )

        self.assertIs(raised.exception, build_error)
        self.session.terminate.assert_called_once_with()
        self.session.close.assert_called_once_with()

    def test_get_port_url(self):
        with patch.dict(
            os.environ,
            {"AKERNEL_SERVER_ADDRESS": "gateway.example.com"},
            clear=True,
        ):
            sandbox = Sandbox(port_forwardings=[8080])
            self.assertEqual(
                sandbox.get_port_url(8080),
                "http://gateway.example.com/physical-id/8080",
            )
            with self.assertRaisesRegex(ValueError, "not in port_forwardings"):
                sandbox.get_port_url(9090)
            sandbox.kill()


if __name__ == "__main__":
    unittest.main()
