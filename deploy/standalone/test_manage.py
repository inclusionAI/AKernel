# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Exercise source-profile safety and recovery without a Docker daemon."""

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "standalone_manage", Path(__file__).resolve().with_name("manage.py")
)
manage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manage)

REVISION = "1" * 40
COMPONENT_REVISION = "2" * 40
CONTAINER_IDS = {"akernel-node": "a" * 64, "akernel-traefik": "b" * 64}
IMAGE_ID = "sha256:" + "c" * 64
EMPTY_SANDBOX_LIST = "ID STATUS RUNTIME AGE CREATED"


class LocalBackend:
    """Model the observable Git and Docker responses, never host executables."""

    def __init__(self, manager):
        self.manager = manager
        self.events = []
        self.info = {"OSType": "linux", "Architecture": "x86_64", "OperatingSystem": "Ubuntu"}
        self.endpoint = "unix:///var/run/docker.sock"
        self.containers = {}
        self.objects = {}
        self.inventory_override = None
        self.inventory_failure = False
        self.component_revision = COMPONENT_REVISION
        self.component_dirty = ""
        self.root_dirty = ""
        self.build_failure = False
        self.start_failure = False
        self.stop_failure = False
        self.image_architecture = None
        self.image_revision = None
        self.sandbox_listing = EMPTY_SANDBOX_LIST
        self.sandbox_listing_failure = False

    def record(self, kind, args):
        self.events.append({"kind": kind, "args": args, "env": dict(self.manager.env)})

    def capture(self, *args):
        self.record("capture", args)
        if args[:3] == ("docker", "context", "show"):
            return "fixture-local"
        if args[:3] == ("docker", "context", "inspect"):
            return json.dumps([{"Endpoints": {"docker": {"Host": self.endpoint}}}])
        if args[:2] == ("docker", "info"):
            return json.dumps(self.info)
        if args[:2] == ("docker", "ps"):
            if self.inventory_failure:
                raise manage.Failure("container inventory unavailable")
            if self.inventory_override is not None:
                return self.inventory_override
            return "\n".join(json.dumps({"Names": name, "ID": container_id})
                             for name, container_id in self.containers.items())
        if args[:3] == ("docker", "image", "inspect"):
            return json.dumps([{
                "Id": IMAGE_ID, "Os": "linux",
                "Architecture": self.image_architecture or self.manager.env["AKERNEL_TARGETARCH"],
                "Config": {"Labels": {"org.opencontainers.image.revision":
                                      self.image_revision or self.manager.state["revision"]}},
            }])
        if args[:2] == ("docker", "inspect"):
            return json.dumps([self.objects[args[2]]])
        if args[:2] == ("docker", "exec"):
            if self.sandbox_listing_failure:
                raise manage.Failure("sandboxd is unavailable")
            return self.sandbox_listing
        if args[:3] == ("git", "ls-tree", "HEAD"):
            return f"160000 commit {COMPONENT_REVISION}\tsrc/sandboxd"
        if args[:2] == ("git", "-C"):
            if args[3:] == ("rev-parse", "HEAD"):
                return self.component_revision
            if args[3:] == ("status", "--porcelain=v1"):
                return self.component_dirty
        if args == ("git", "rev-parse", "HEAD"):
            return REVISION
        if args == ("git", "status", "--porcelain=v1"):
            return self.root_dirty
        if args[0] == "curl":
            return "ok"
        raise AssertionError(f"unexpected external read: {args}")

    def run(self, *args):
        self.record("run", args)
        if Path(args[1]).name == "build-image.sh":
            if self.build_failure:
                raise manage.Failure("fixture build failed")
            return
        if Path(args[1]).name == "start.sh":
            self.add_container("akernel-node")
            if self.start_failure:
                raise manage.Failure("fixture start failed after node creation")
            self.add_container("akernel-traefik")
            return
        if Path(args[1]).name == "stop.sh":
            if self.stop_failure:
                raise manage.Failure("fixture stop failed")
            self.containers.clear()
            self.objects.clear()
            return
        if args[:3] == ("git", "submodule", "update"):
            (self.manager.root / "src/sandboxd/.git").write_text("gitdir: fixture\n")
            return
        raise AssertionError(f"unexpected external write: {args}")

    def add_container(self, name):
        container_id = CONTAINER_IDS[name]
        self.containers[name] = container_id
        source, destination = (
            (str(self.manager.data), "/home/akernel")
            if name == "akernel-node"
            else (str(self.manager.data / "traefik/dynamic.yml"), "/etc/traefik/dynamic.yml")
        )
        self.objects[container_id] = {
            "Id": container_id, "Image": IMAGE_ID, "State": {"Running": True},
            "Mounts": [{"Type": "bind", "Source": source, "Destination": destination}],
            "NetworkSettings": {"Networks": {"bridge": {"IPAddress": "192.0.2.44"}}},
        }

    def runs(self, script):
        return [event for event in self.events if event["kind"] == "run"
                and Path(event["args"][1]).name == script]


class ManageTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.component = self.root / "src/sandboxd"
        self.component.mkdir(parents=True)
        (self.component / ".git").write_text("gitdir: fixture\n")
        self.manager = manage.Manager(self.root, {"PATH": "/fixture/bin"})
        self.backend = LocalBackend(self.manager)
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        for context in (
            patch.object(self.manager, "capture", self.backend.capture),
            patch.object(self.manager, "run", self.backend.run),
            patch.object(manage.shutil, "which", lambda command, **kwargs: f"/fixture/bin/{command}"),
            patch.object(manage.platform, "system", return_value="Linux"),
            redirect_stdout(self.stdout), redirect_stderr(self.stderr),
        ):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)

    def saved_state(self):
        return json.loads(self.manager.state_file.read_text())

    def create_profile(self):
        self.manager.state = {
            "data_dir": str(self.manager.data), "revision": REVISION,
            "image": "akernel-local/all-in-one:fixture", "image_id": IMAGE_ID,
            "status": "ready", "containers": dict(CONTAINER_IDS),
        }
        self.manager.save()
        for name in CONTAINER_IDS:
            self.backend.add_container(name)
        token = self.manager.data / "token"
        token.write_text("fixture-private-token\n")
        token.chmod(0o600)
        return token

    def assert_up_untouched(self):
        self.assertFalse(self.backend.runs("build-image.sh"))
        self.assertFalse(self.backend.runs("start.sh"))
        self.assertFalse(self.manager.data.exists())

    def test_inventory_failure_or_malformed_inventory_cannot_build(self):
        cases = (
            (True, None),
            (False, "fixture-private-diagnostic"),
            (False, json.dumps({"Names": "akernel-node"})),
            (False, json.dumps({"Names": "akernel-node", "ID": "truncated"})),
            (False, "\n".join([json.dumps({"Names": "akernel-node", "ID": CONTAINER_IDS["akernel-node"]})] * 2)),
        )
        for failure, output in cases:
            with self.subTest(failure=failure, output=output):
                self.backend.inventory_failure = failure
                self.backend.inventory_override = output
                with self.assertRaises((manage.Failure, ValueError, KeyError)):
                    self.manager.up()
                self.assert_up_untouched()

    def test_cli_inventory_diagnostic_does_not_print_private_output(self):
        self.backend.inventory_override = "fixture-private-diagnostic"
        with patch.object(manage, "Manager", return_value=self.manager), patch.object(manage.sys, "argv", ["manage.py", "up"]):
            with self.assertRaises(SystemExit) as result:
                manage.main()
        self.assertEqual(result.exception.code, 1)
        self.assertNotIn("fixture-private-diagnostic", self.stdout.getvalue() + self.stderr.getvalue())
        self.assert_up_untouched()

    def test_existing_named_container_is_rejected_even_when_stopped(self):
        for name in CONTAINER_IDS:
            for running in (False, True):
                with self.subTest(name=name, running=running):
                    self.backend.containers.clear()
                    self.backend.objects.clear()
                    self.backend.add_container(name)
                    self.backend.objects[CONTAINER_IDS[name]]["State"]["Running"] = running
                    with self.assertRaises(manage.Failure):
                        self.manager.up()
                    self.assert_up_untouched()
                    self.assertFalse(self.backend.runs("stop.sh"))

    def test_native_target_follows_local_linux_daemon_or_orbstack(self):
        cases = (("Linux", "Ubuntu", "x86_64", "amd64"),
                 ("Linux", "Ubuntu", "amd64", "amd64"),
                 ("Linux", "Ubuntu", "aarch64", "arm64"),
                 ("Linux", "Ubuntu", "arm64", "arm64"),
                 ("Darwin", "OrbStack", "aarch64", "arm64"))
        self.manager.env["DOCKER_DEFAULT_PLATFORM"] = "linux/amd64"
        for host, engine, architecture, expected in cases:
            with self.subTest(host=host, engine=engine, architecture=architecture):
                self.backend.info.update(OperatingSystem=engine, Architecture=architecture)
                with patch.object(manage.platform, "system", return_value=host):
                    self.assertEqual(self.manager.native_arch(), expected)

    def test_platform_mismatch_or_non_native_mac_daemon_cannot_build(self):
        cases = (
            ("Linux", "linux", "amd64", "Ubuntu", "arm64"),
            ("Linux", "linux", "arm64", "Ubuntu", "amd64"),
            ("Linux", "windows", "amd64", "Windows", None),
            ("Linux", "linux", "riscv64", "Ubuntu", None),
            ("Darwin", "linux", "arm64", "Docker Desktop", None),
            ("Darwin", "linux", "amd64", "OrbStack", None),
        )
        for host, os_type, architecture, engine, target in cases:
            with self.subTest(host=host, architecture=architecture, target=target):
                self.backend.info.update(OSType=os_type, Architecture=architecture, OperatingSystem=engine)
                self.manager.env.pop("AKERNEL_TARGETARCH", None)
                if target:
                    self.manager.env["AKERNEL_TARGETARCH"] = target
                with patch.object(manage.platform, "system", return_value=host):
                    with self.assertRaises(manage.Failure):
                        self.manager.up()
                self.assert_up_untouched()

    def test_source_build_and_launch_override_conflicting_client_platform(self):
        for architecture, client_platform in (("amd64", "linux/arm64"),
                                               ("arm64", "linux/amd64")):
            with self.subTest(architecture=architecture):
                self.backend.events.clear()
                self.backend.containers.clear()
                self.backend.objects.clear()
                self.backend.info["Architecture"] = architecture
                self.manager.env.pop("AKERNEL_TARGETARCH", None)
                self.manager.env["DOCKER_DEFAULT_PLATFORM"] = client_platform
                self.manager.up()
                for script in ("build-image.sh", "start.sh"):
                    forwarded = self.backend.runs(script)[0]["env"]
                    self.assertEqual(forwarded["AKERNEL_TARGETARCH"], architecture)
                    self.assertEqual(forwarded["DOCKER_DEFAULT_PLATFORM"], f"linux/{architecture}")
                self.assertEqual(self.saved_state()["platform"], f"linux/{architecture}")

    def test_remote_context_and_remote_docker_host_cannot_build(self):
        for endpoint in ("ssh://fixture-host", "tcp://127.0.0.1:2375"):
            for direct_host in (False, True):
                with self.subTest(endpoint=endpoint, direct_host=direct_host):
                    self.manager.env.pop("DOCKER_HOST", None)
                    self.backend.endpoint = endpoint
                    if direct_host:
                        self.manager.env["DOCKER_HOST"] = endpoint
                    with self.assertRaises(manage.Failure):
                        self.manager.up()
                    self.assert_up_untouched()

    def test_default_build_is_runsc_only_and_does_not_load_cloud_profile(self):
        cloud = self.root / ".akernel/default/config.env"
        cloud.parent.mkdir(parents=True)
        content = "IMAGE_REPOSITORY=fixture-private-registry\nAKERNEL_ENABLE_KATA=true\nAKERNEL_ENABLE_FIRECRACKER=true\nAKERNEL_ENABLE_RUNC=true\n"
        cloud.write_text(content)
        self.manager.env["ENV"] = "default"
        self.manager.up()
        build = self.backend.runs("build-image.sh")[0]
        self.assertNotIn("--env", build["args"])
        for runtime in ("KATA", "FIRECRACKER", "RUNC", "GPU"):
            self.assertEqual(build["env"][f"AKERNEL_ENABLE_{runtime}"], "false")
        self.assertEqual(build["env"]["AKERNEL_TARGETARCH"], "amd64")
        self.assertEqual(build["args"][build["args"].index("--runtime-profile") + 1], "rrt")
        self.assertNotIn("fixture-private-registry", " ".join(build["args"]))
        self.assertEqual(cloud.read_text(), content)
        self.assertEqual(self.saved_state()["status"], "ready")

    def test_explicit_runc_is_forwarded_without_enabling_vm_payloads(self):
        self.manager.env["AKERNEL_ENABLE_RUNC"] = "true"
        resolver = self.root / "approved-resolver.conf"
        resolver.write_text("nameserver 192.0.2.53\n")
        self.manager.env["AKERNEL_RUNC_RESOLV_CONF"] = str(resolver)
        self.manager.directory = Path(__file__).resolve().parent
        self.manager.up()
        build = self.backend.runs("build-image.sh")[0]
        self.assertEqual(build["env"]["AKERNEL_ENABLE_RUNC"], "true")
        for name in ("KATA", "FIRECRACKER", "GPU"):
            self.assertEqual(build["env"][f"AKERNEL_ENABLE_{name}"], "false")

    def test_dirty_or_misaligned_component_is_preserved_without_checkout(self):
        sentinel = self.component / "developer-work"
        sentinel.write_text("preserve existing work\n")
        cases = ((COMPONENT_REVISION, " M runtime.go"),
                 (COMPONENT_REVISION, "?? developer-work"),
                 ("3" * 40, ""))
        for revision, dirty in cases:
            with self.subTest(revision=revision, dirty=dirty):
                self.backend.component_revision = revision
                self.backend.component_dirty = dirty
                with self.assertRaises(manage.Failure):
                    self.manager.up()
                self.assert_up_untouched()
                self.assertFalse(any(event["kind"] == "run" for event in self.backend.events))
                self.assertEqual(sentinel.read_text(), "preserve existing work\n")

    def test_nonempty_uninitialized_component_is_not_overwritten(self):
        (self.component / ".git").unlink()
        sentinel = self.component / "local-source.go"
        sentinel.write_text("uncommitted source\n")
        with self.assertRaises(manage.Failure):
            self.manager.up()
        self.assert_up_untouched()
        self.assertFalse(any(event["kind"] == "run" for event in self.backend.events))
        self.assertEqual(sentinel.read_text(), "uncommitted source\n")

    def test_build_failure_is_recorded_and_can_be_retried(self):
        self.backend.build_failure = True
        with self.assertRaises(manage.Failure):
            self.manager.up()
        failed = self.saved_state()
        self.assertEqual(failed["status"], "failed")
        self.assertNotIn("image_id", failed)
        self.assertFalse(self.backend.runs("start.sh"))
        self.backend.build_failure = False
        self.manager.up()
        ready = self.saved_state()
        self.assertEqual(ready["status"], "ready")
        self.assertNotEqual(ready["image"], failed["image"])
        self.assertEqual(ready["containers"], CONTAINER_IDS)

    def test_built_image_must_match_selected_architecture_and_source(self):
        for field, value in (("image_architecture", "arm64"), ("image_revision", "3" * 40)):
            with self.subTest(field=field):
                setattr(self.backend, field, value)
                with self.assertRaises(manage.Failure):
                    self.manager.up()
                self.assertEqual(self.saved_state()["status"], "failed")
                self.assertFalse(self.backend.runs("start.sh"))
                setattr(self.backend, field, None)

    def test_partial_start_failure_can_be_inspected_stopped_and_retried(self):
        self.backend.start_failure = True
        with self.assertRaises(manage.Failure):
            self.manager.up()
        self.assertEqual(self.saved_state()["status"], "failed")
        self.assertEqual(set(self.backend.containers), {"akernel-node"})
        with self.assertRaises(manage.Failure):
            self.manager.status()
        self.assertIn("akernel-node: running", self.stdout.getvalue())
        self.assertIn("akernel-traefik: stopped or absent", self.stdout.getvalue())
        self.manager.stop()
        stop = self.backend.runs("stop.sh")[0]
        self.assertEqual(stop["env"]["AKERNEL_EXPECTED_NODE_ID"], CONTAINER_IDS["akernel-node"])
        self.assertEqual(stop["env"]["AKERNEL_EXPECTED_TRAEFIK_ID"], "absent")
        self.assertEqual(self.saved_state()["status"], "stopped")
        self.backend.start_failure = False
        self.manager.up()
        self.assertEqual(self.saved_state()["status"], "ready")

    def test_stop_rejects_replaced_container_ids_before_any_shutdown(self):
        self.create_profile()
        for name in CONTAINER_IDS:
            with self.subTest(name=name):
                original = self.backend.containers[name]
                self.backend.containers[name] = "d" * 64
                with self.assertRaises(manage.Failure):
                    self.manager.stop()
                self.assertFalse(self.backend.runs("stop.sh"))
                self.assertEqual(self.saved_state()["status"], "ready")
                self.backend.containers[name] = original

    def test_stop_validates_both_bind_mounts_before_any_shutdown(self):
        self.create_profile()
        for name in CONTAINER_IDS:
            for field, value in (("Source", "/another-profile"), ("Destination", "/another-mount"), ("Type", "volume")):
                with self.subTest(name=name, field=field):
                    mount = self.backend.objects[CONTAINER_IDS[name]]["Mounts"][0]
                    original = mount[field]
                    mount[field] = value
                    with self.assertRaises(manage.Failure):
                        self.manager.stop()
                    self.assertFalse(self.backend.runs("stop.sh"))
                    self.assertFalse(any(event["args"][:2] == ("docker", "exec") for event in self.backend.events))
                    mount[field] = original

    def test_stop_rejects_node_with_wrong_image(self):
        self.create_profile()
        self.backend.objects[CONTAINER_IDS["akernel-node"]]["Image"] = "sha256:" + "d" * 64
        with self.assertRaises(manage.Failure):
            self.manager.stop()
        self.assertFalse(self.backend.runs("stop.sh"))

    def test_active_sandbox_or_unavailable_inventory_prevents_stop(self):
        self.create_profile()
        for listing, failed in ((EMPTY_SANDBOX_LIST + "\nfixture-sandbox RUNNING runsc 1m now", False),
                                ("", False), ("unrecognized output", False), (EMPTY_SANDBOX_LIST, True)):
            with self.subTest(listing=listing, failed=failed):
                self.backend.sandbox_listing = listing
                self.backend.sandbox_listing_failure = failed
                with self.assertRaises(manage.Failure):
                    self.manager.stop()
                self.assertFalse(self.backend.runs("stop.sh"))
                self.assertEqual(self.saved_state()["status"], "ready")

    def test_successful_stop_checks_every_container_then_retains_profile_data(self):
        token = self.create_profile()
        self.manager.stop()
        stop = self.backend.runs("stop.sh")[0]
        stop_position = self.backend.events.index(stop)
        for container_id in CONTAINER_IDS.values():
            inspection = next(index for index, event in enumerate(self.backend.events)
                              if event["args"] == ("docker", "inspect", container_id))
            self.assertLess(inspection, stop_position)
        self.assertEqual(stop["env"]["AKERNEL_EXPECTED_NODE_ID"], CONTAINER_IDS["akernel-node"])
        self.assertEqual(stop["env"]["AKERNEL_EXPECTED_TRAEFIK_ID"], CONTAINER_IDS["akernel-traefik"])
        self.assertEqual(self.saved_state()["status"], "stopped")
        self.assertEqual(token.read_text(), "fixture-private-token\n")
        self.manager.stop()
        repeated = self.backend.runs("stop.sh")[-1]
        self.assertEqual(repeated["env"]["AKERNEL_EXPECTED_NODE_ID"], "absent")
        self.assertEqual(repeated["env"]["AKERNEL_EXPECTED_TRAEFIK_ID"], "absent")

    def test_stop_failure_does_not_report_stopped_and_can_be_retried(self):
        self.create_profile()
        self.backend.stop_failure = True
        with self.assertRaises(manage.Failure):
            self.manager.stop()
        self.assertEqual(self.saved_state()["status"], "ready")
        self.backend.stop_failure = False
        self.manager.stop()
        self.assertEqual(self.saved_state()["status"], "stopped")

    def test_sdk_environment_reads_token_at_use_time_without_saving_its_value(self):
        token = self.create_profile()
        self.manager.sdk_environment("192.0.2.44")
        sdk = self.manager.data / "sdk-env.sh"
        self.assertIn(str(token), sdk.read_text())
        self.assertIn("192.0.2.44", sdk.read_text())
        self.assertEqual(sdk.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("fixture-private-token", sdk.read_text() + self.manager.state_file.read_text()
                         + self.stdout.getvalue() + self.stderr.getvalue())

    def test_competing_operation_cannot_read_backend_or_change_profile_metadata(self):
        token = self.create_profile()
        original_state = self.manager.state_file.read_bytes()
        original_env = dict(self.manager.env)
        owner = manage.Manager(self.root, self.manager.env)
        # Real file locks must reject separately opened descriptors, including
        # status readers, while an operation owns this profile.
        with owner.lock():
            for command in ("up", "status", "stop"):
                with self.subTest(command=command):
                    with self.assertRaisesRegex(manage.Failure, "another operation"):
                        getattr(self.manager, command)()
                    self.assertEqual(self.backend.events, [])
                    self.assertEqual(self.manager.state_file.read_bytes(), original_state)
                    self.assertEqual(self.manager.env, original_env)
                    self.assertEqual(token.read_text(), "fixture-private-token\n")

    def test_failed_operation_releases_lock_for_a_new_manager(self):
        self.backend.build_failure = True
        with self.assertRaises(manage.Failure):
            self.manager.up()
        self.assertEqual(self.saved_state()["status"], "failed")
        retry = manage.Manager(self.root, self.manager.env)
        backend = LocalBackend(retry)
        with patch.object(retry, "capture", backend.capture), patch.object(retry, "run", backend.run):
            retry.up()
        self.assertEqual(len(backend.runs("build-image.sh")), 1)
        self.assertEqual(self.saved_state()["status"], "ready")
        self.assertEqual(self.saved_state()["containers"], CONTAINER_IDS)


if __name__ == "__main__":
    unittest.main()
