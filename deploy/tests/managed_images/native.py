"""Owned native Nydus/Compose checkpoint qualification; never targets a deployment.

Requires a complete matching all-in-one image, the current managed-tools image,
and cached input/Registry/Nydus-BuildKit images. Every service, namespace, image
publication and backing filesystem used here is newly created by this process.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import io
import json
import os
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path

import httpx
import yaml
from akernel_sdk import (
    BackendOperationError,
    Client,
    ComposeProject,
    DockerRuntime,
    ImageRetention,
    build_image,
)

REPO = Path(__file__).resolve().parents[3]
module = importlib.util.spec_from_file_location(
    "managed_image_reclaimer", REPO / "builder/managed-docker/reclaim.py"
)
reclaimer = importlib.util.module_from_spec(module)
module.loader.exec_module(reclaimer)

WITNESS = """
import json,os,pathlib,secrets,time
root=pathlib.Path('/tmp'); launch=root/'native-launches'
launch.write_text(str(int(launch.read_text())+1 if launch.exists() else 1))
cookie=secrets.token_hex(32); sequence=0
while True:
 sequence+=1
 value={'cookie':cookie,'sequence':sequence,'pid':os.getpid(),'launches':int(launch.read_text())}
 path=root/'native-witness.next'; path.write_text(json.dumps(value));path.replace(root/'native-witness.json')
 time.sleep(.1)
"""


def command(*args):
    return subprocess.run(
        args, check=True, capture_output=True, text=True
    ).stdout.strip()


def write(path, value):
    path.write_text(json.dumps(value, indent=2))
    path.chmod(0o600)


def unused_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def allocated(path):
    return sum(p.stat().st_blocks * 512 for p in path.rglob("*") if p.is_file())


def layer_media_type(data):
    """Docker's image store may save compressed bytes even inside Docker archives."""
    media = "application/vnd.oci.image.layer.v1.tar"
    if data.startswith(b"\x1f\x8b\x08"):
        return media + "+gzip"
    if data.startswith(b"\x28\xb5\x2f\xfd"):
        return media + "+zstd"
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:"):
        return media


def wait_for(check, seconds=90):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (httpx.HTTPError, OSError, RuntimeError, BackendOperationError) as error:
            last = error
        time.sleep(0.25)
    raise RuntimeError("owned qualification readiness/condition timed out") from last


class Fixture:
    def __init__(self, args):
        self.args = args
        self.run_id = uuid.uuid4().hex[:12]
        self.name = "akernel-mi167-" + self.run_id
        self.output = args.output.absolute()
        self.output.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.root = args.data_root.absolute()
        self.data = self.root / "data"
        self.containers = []
        self.network = None
        self.loop = None
        self.mounted = False
        self.client = None
        self.sandboxes = []
        self.snapshots = []
        self.publications = []
        self.results = {"status": "failed"}
        self.registry_password = secrets.token_hex(32)
        self.registry_auth = ("qualification", self.registry_password)
        self.password_hash = ""

    def phase(self, name):
        self.results["phase"] = name
        write(self.output / "progress.json", self.results)
        print("native qualification: " + name, flush=True)

    def receipt(self):
        write(
            self.output / "resources.json",
            {
                "run_id": self.run_id,
                "data": str(self.data),
                "loop": self.loop,
                "backing": str(self.root / "native-xfs.img"),
                "mounted": self.mounted,
                "network": self.network,
                "containers": self.containers,
            },
        )

    def check_loop(self):
        if command(
            "losetup", "--noheadings", "--output", "BACK-FILE", self.loop
        ) != str(self.root / "native-xfs.img"):
            raise RuntimeError("owned loop identity changed")

    def start(self, role, *args):
        name = self.name + "-" + role
        identity = command(
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--label",
            "akernel.task=managed-images-167",
            "--label",
            "akernel.qualification=" + self.run_id,
            "--network",
            self.network,
            *args,
        )
        self.containers.append({"role": role, "name": name, "id": identity})
        self.receipt()
        return identity

    def inspect(self, identity):
        value = json.loads(command("docker", "inspect", identity))[0]
        if value["Config"]["Labels"]["akernel.qualification"] != self.run_id:
            raise RuntimeError("owned container identity changed")
        return value

    def seed(self, image, repository):
        """Publish a selected local Docker archive without changing daemon auth."""
        archive_path = self.data / (repository.replace("/", "-") + ".tar")
        command("docker", "image", "save", "--output", str(archive_path), image)
        with (
            tarfile.open(archive_path) as archive,
            httpx.Client(timeout=300, auth=self.registry_auth) as http,
        ):
            entries = json.load(archive.extractfile("manifest.json"))
            if len(entries) != 1:
                raise ValueError("one native image required")
            entry = entries[0]

            def blob(path, media):
                data = archive.extractfile(path).read()
                if media == "application/vnd.oci.image.layer.v1.tar":
                    media = layer_media_type(data)
                hash_value = digest(data)
                response = http.post(
                    f"{self.registry_url}/v2/{repository}/blobs/uploads/"
                )
                response.raise_for_status()
                location = response.headers["Location"]
                if location.startswith("/"):
                    location = self.registry_url + location
                if not location.startswith(self.registry_url + "/"):
                    raise ValueError("unexpected Registry upload origin")
                response = http.put(
                    httpx.URL(location).copy_merge_params({"digest": hash_value}),
                    content=data,
                    headers={"Content-Type": "application/octet-stream"},
                )
                response.raise_for_status()
                return {"mediaType": media, "digest": hash_value, "size": len(data)}

            manifest = {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": blob(
                    entry["Config"], "application/vnd.oci.image.config.v1+json"
                ),
                "layers": [
                    blob(p, "application/vnd.oci.image.layer.v1.tar")
                    for p in entry["Layers"]
                ],
            }
            raw = json.dumps(manifest, separators=(",", ":")).encode()
            response = http.put(
                f"{self.registry_url}/v2/{repository}/manifests/fixture",
                content=raw,
                headers={"Content-Type": manifest["mediaType"]},
            )
            response.raise_for_status()
            return f"{self.registry_authority}/{repository}@{digest(raw)}"

    def setup(self):
        self.phase("prepare fresh owned filesystem")
        if (
            command("findmnt", "-n", "-o", "UUID", "-T", str(self.root.parent))
            != self.args.disk_uuid
        ):
            raise RuntimeError("dedicated data disk identity mismatch")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.data.exists() and any(self.data.iterdir()):
            raise RuntimeError("native data must be fresh and empty")
        self.data.mkdir(mode=0o700, exist_ok=True)
        if (
            subprocess.run(["mountpoint", "-q", str(self.data)], check=False).returncode
            == 0
        ):
            raise RuntimeError("data path is already a mountpoint")
        backing = self.root / "native-xfs.img"
        if not self.password_hash:
            raise RuntimeError(
                "native Registry requires a prepared bcrypt password hash"
            )
        with backing.open("xb") as stream:
            stream.truncate(32 * 1024**3)
        self.loop = command("losetup", "--find", "--show", str(backing))
        self.receipt()
        if command(
            "losetup", "--noheadings", "--output", "BACK-FILE", self.loop
        ) != str(backing):
            raise RuntimeError("loop backing identity mismatch")
        command("mkfs.xfs", "-m", "reflink=1", self.loop)
        command("mount", "-o", "noatime", self.loop, str(self.data))
        self.mounted = True
        self.receipt()
        for folder in ("registry", "buildkit", "config"):
            (self.data / folder).mkdir(mode=0o700)
        self.network = command(
            "docker",
            "network",
            "create",
            "--label",
            "akernel.qualification=" + self.run_id,
            self.name,
        )
        self.receipt()
        registry_config = self.data / "registry.yaml"
        registry_config.write_text(
            "version: 0.1\nstorage:\n  filesystem:\n    rootdirectory: /var/lib/registry\n  delete:\n    enabled: true\nhttp:\n  addr: :5000\nauth:\n  htpasswd:\n    realm: qualification\n    path: /etc/distribution/htpasswd\n"
        )
        auth_path = self.data / "htpasswd"
        auth_path.write_text("qualification:" + self.password_hash + "\n")
        auth_path.chmod(0o600)
        self.registry = self.start(
            "registry",
            "--memory",
            "512m",
            "--cpus",
            "1",
            "-v",
            f"{self.data / 'registry'}:/var/lib/registry",
            "-v",
            f"{registry_config}:/etc/distribution/config.yml:ro",
            "-v",
            f"{auth_path}:/etc/distribution/htpasswd:ro",
            self.args.registry_image,
        )
        ip = self.inspect(self.registry)["NetworkSettings"]["Networks"][self.name][
            "IPAddress"
        ]
        self.registry_authority = ip + ":5000"
        self.registry_url = "http://" + self.registry_authority
        # Only this fixture process bypasses inherited proxies for its owned network.
        bypass = ",".join(
            filter(None, [os.environ.get("NO_PROXY", ""), "127.0.0.1", "localhost", ip])
        )
        os.environ["NO_PROXY"] = bypass
        os.environ["no_proxy"] = bypass
        wait_for(
            lambda: (
                httpx.get(
                    self.registry_url + "/v2/", auth=self.registry_auth, timeout=2
                ).status_code
                == 200
            )
        )
        self.phase("seed owned Registry inputs")
        self.tools = self.seed(self.args.tool_image, "fixtures/tools")
        self.input = self.seed(self.args.input_image, "fixtures/input")
        bk_config = self.data / "buildkit.toml"
        bk_config.write_text(
            f'[registry."{self.registry_authority}"]\n  http = true\n  insecure = true\n'
        )
        self.buildkit = self.start(
            "buildkit",
            "--privileged",
            "--memory",
            "4g",
            "--cpus",
            "4",
            "-v",
            f"{self.data / 'buildkit'}:/var/lib/buildkit",
            "-v",
            f"{bk_config}:/etc/buildkit/buildkitd.toml:ro",
            self.args.buildkit_image,
            "--addr",
            "tcp://0.0.0.0:1234",
            "--oci-worker-snapshotter",
            "native",
        )
        bk_ip = self.inspect(self.buildkit)["NetworkSettings"]["Networks"][self.name][
            "IPAddress"
        ]
        config = self.data / "config"
        for path in (REPO / "deploy/standalone/config").iterdir():
            if path.is_file():
                shutil.copyfile(path, config / path.name)
        registry_template = json.loads((config / "registry.json").read_text())
        registry_template["registry"].update(scheme="http", blob_url_scheme="http")
        write(config / "registry.json", registry_template)
        write(
            config / "registry_auths.json",
            {
                "auths": {
                    self.registry_authority: {
                        "auth": base64.b64encode(
                            ("qualification:" + self.registry_password).encode()
                        ).decode()
                    }
                }
            },
        )
        text = (config / "sandboxd_config.toml").read_text()
        text = text.replace('filestore_dir_size="50G"', 'filestore_dir_size=""')
        text = text.replace('overlay_tmpfs_size="10G"', 'overlay_tmpfs_size="256M"')
        text = (
            text.replace("cgroup_cache_size=800", "cgroup_cache_size=2")
            .replace("netns_cache_size=1000", "netns_cache_size=2")
            .replace("interface_cache_size=1000", "interface_cache_size=2")
        )
        text = text.replace(
            "[plugin.image]",
            '[plugin.image]\nmanagement_socket="/run/sandboxd/image-api/image.sock"\nchunk_db_size="4GiB"',
        )
        text = text.replace(
            "[plugin.runtime.runsc]",
            '[plugin.runtime.runsc]\nplatform="systrap"\ncheckpoint_mode="reflink"\nextra_args=["--net-raw=true", "--allow-packet-socket-write=true"]',
        )
        text += f'''
[plugin.managed_runtime]
root="/home/akernel/managed-runtime"
[plugin.managed_runtime.profiles.qualification]
image="{self.tools}"
bootstrap_root="/opt/akernel/runtime/akernel-runtime-rootfs.img"
registry="{self.registry_authority}"
repository_prefix="managed"
registry_auth_file="/home/akernel/sandboxd/config/registry_auths.json"
registry_insecure=true
buildkit_endpoint="tcp://{bk_ip}:1234"
provider_url="http://10.88.0.1:8840"
allowed_tenants=["admin"]
writable_mb=4096
minimum_root_mb=4096
'''
        (config / "sandboxd_config.toml").write_text(text)
        command(
            "bash",
            "-euc",
            """
source "$1/deploy/standalone/start.sh"
CONFIG_DIR="$2"; DATA_DIR="$3"; TOKEN_FILE="$3/token"
SANDBOXD_CONFIG_FILE="$3/sandboxd/config.toml"
prepare_data
configure_auth
configure_network
""",
            "owned-native-config",
            str(REPO),
            str(config),
            str(self.data),
        )
        for path in config.glob("*.json"):
            shutil.copyfile(path, self.data / "sandboxd/config" / path.name)
        (self.data / "images").mkdir(exist_ok=True)
        shutil.copyfile(config / "config.json", self.data / "images/config.json")
        deployment = yaml.safe_load(
            (REPO / "builder/config/adx-standalone.yaml").read_text()
        )
        deployment["namespace"] = self.name
        overrides = deployment["service_overrides"]
        overrides["coordinator"]["config"]["managed_image_reclamation"] = True
        overrides["coordinator"]["config"]["managed_images"] = {
            "registry": {
                "authority": self.registry_authority,
                "repository_prefix": "managed",
            },
        }
        overrides["adxlet"]["config"]["labels"] = {
            "AKERNEL_RUNTIME_PROFILE_qualification": "8192"
        }
        profile_path = self.data / "adx-qualification.yaml"
        profile_path.write_text(yaml.safe_dump(deployment))
        env_path = self.data / "managed-runtime.env"
        env_path.write_text(
            "MANAGED_RUNTIME_ROOT=/home/akernel/managed-runtime\nMANAGED_IMAGE_SOCKET=/run/sandboxd/image-api/image.sock\nMANAGED_IMAGE_PORT=8840\n"
        )
        self.control_port = unused_port()
        self.data_port = unused_port()
        self.node = self.start(
            "node",
            "--privileged",
            "--memory",
            "16g",
            "--cpus",
            "8",
            "-p",
            f"127.0.0.1:{self.control_port}:8443",
            "-p",
            f"127.0.0.1:{self.data_port}:8080",
            "-e",
            "container=oci",
            "-e",
            "AKERNEL_ROLE=standalone",
            "-e",
            "AKS_LOCAL_MODE=true",
            "-e",
            "NODE_NAME=" + self.name,
            "-e",
            "POD_NAME=" + self.name,
            "-e",
            "AKERNEL_ADX_CONFIG=/etc/akernel/adx-qualification.yaml",
            "-v",
            f"{self.data}:/home/akernel",
            "-v",
            f"{self.data / 'sandboxd/config.toml'}:/home/akernel/sandboxd/config.toml:ro",
            "-v",
            f"{profile_path}:/etc/akernel/adx-qualification.yaml:ro",
            "-v",
            f"{env_path}:/etc/akernel/managed-runtime.env:ro",
            "--entrypoint",
            "/usr/local/bin/akernel-entrypoint",
            self.args.node_image,
        )
        wait_for(
            lambda: (
                subprocess.run(
                    ["docker", "exec", self.node, "test", "-S", "/run/systemd/private"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                ).returncode
                == 0
            )
        )
        command(
            "docker",
            "exec",
            self.node,
            "systemctl",
            "enable",
            "--now",
            "akernel-managed-images.service",
        )
        self.client = Client(
            server_address=f"https://127.0.0.1:{self.control_port}",
            gateway_address=f"http://127.0.0.1:{self.data_port}",
            token=(self.data / "token").read_text().strip(),
            verify_tls=False,
        )
        self.phase("wait for owned backend capacity")
        wait_for(lambda: self.ready(), seconds=120)
        self.phase("owned backend ready")
        write(
            self.output / "inputs.json",
            {
                "node_image": self.inspect(self.node)["Image"],
                "tools": self.tools,
                "input": self.input,
                "registry_image": self.inspect(self.registry)["Image"],
                "buildkit_image": self.inspect(self.buildkit)["Image"],
                "namespace": self.name,
            },
        )

    def ready(self):
        try:
            list(self.client.images.list())
            response = httpx.get(
                f"https://127.0.0.1:{self.control_port}/api/environment/v1/resources",
                headers={"X-Auth": (self.data / "token").read_text().strip()},
                verify=False,
                timeout=3,
            )
            response.raise_for_status()
            return any(
                item.get("allocatable", {}).get("CPU", 0) > 0
                and item.get("allocatable", {}).get("Memory", 0) > 0
                for item in response.json().get("items", [])
            )
        except BackendOperationError:
            return False

    def context(self, name):
        path = self.output / name
        path.mkdir()
        payload = os.urandom(2 * 1024**2)
        (path / "payload").write_bytes(payload)
        (path / "Dockerfile").write_text(
            f"FROM {self.input}\nCOPY payload /image-witness\n"
        )
        return path, hashlib.sha256(payload).hexdigest()

    def observe_protected(self, image, reason):
        image._mutate("observe")
        refs = image.info["image"]["references"]
        if (
            not any(r.startswith(reason + ":") for r in refs)
            or image.info["eligible_for_reclamation"]
        ):
            raise AssertionError(f"expected authoritative {reason} protection")

    def runtime_witness(self, execute):
        def read():
            result = execute("cat /tmp/native-witness.json")
            return json.loads(result.stdout) if result.exit_code == 0 else None

        return wait_for(read, seconds=30)

    def exercise(self, context, expected_hash, *, compose=False):
        self.phase("Compose lifecycle" if compose else "single-image lifecycle")
        client = self.client
        project = None
        if compose:
            compose_path = context / "compose.yaml"
            compose_path.write_text(
                yaml.safe_dump(
                    {
                        "services": {
                            name: {
                                "build": {"context": "."},
                                "command": ["sleep", "infinity"],
                            }
                            for name in ("left", "right")
                        }
                    }
                )
            )
            project = ComposeProject.prepare(
                context,
                [compose_path],
                output_dir=self.output / "compose-prepared",
                runtime=DockerRuntime(profile="qualification"),
                retention=ImageRetention.ephemeral(2),
                request_id=self.run_id + "-compose",
                image_client=client.images,
            )
            image = project.managed_image
            source = client.Sandbox(
                cpu=1000,
                memory=3072,
                idle_timeout=1200,
                schedule_timeout=120,
                **project.sandbox_options(),
            )
            self.sandboxes.append(source.id)
            project.start_runtime(source)
            project.build(timeout=600)
            project.pull(timeout=300)
            project.up(wait_timeout=90)
            service = project.service("left")
            service.start_process(
                shlex.join(["python3", "-c", WITNESS]), shell="/bin/sh"
            )
            execute = service.exec
        else:
            built = build_image(
                context,
                profile="qualification",
                client=client,
                retention=ImageRetention.ephemeral(2),
                request_id=self.run_id + "-single",
                timeout=600,
            )
            image = built.managed_image
            source = client.Sandbox(
                image=f"http://{built.reference}",
                cpu=1000,
                memory=1024,
                idle_timeout=1200,
                schedule_timeout=120,
            )
            self.sandboxes.append(source.id)
            source.commands.run(shlex.join(["python3", "-c", WITNESS]), background=True)
            execute = source.commands.run
        self.publications.append(image.id)
        if execute("sha256sum /image-witness").stdout.split()[0] != expected_hash:
            raise AssertionError("native Nydus image bytes differ")
        before = self.runtime_witness(execute)
        snapshot = source.create_snapshot(
            name=self.name + ("-compose" if compose else "-single"), timeout=300
        )
        self.snapshots.append(snapshot.snapshot_id)
        if execute("touch /tmp/native-after-capture").exit_code != 0:
            raise AssertionError("post-capture source mutation failed")
        image.release()
        self.phase("verify retained Snapshot protection")
        time.sleep(3)
        self.observe_protected(image, "snapshot")
        source.kill()
        self.sandboxes.remove(source.id)
        self.observe_protected(image, "snapshot")
        if not compose:
            command("docker", "exec", self.node, "systemctl", "restart", "adx.service")
            wait_for(self.ready, seconds=120)
            self.observe_protected(image, "snapshot")
        restore_options = (
            {"extra_config": {"managedRuntimeProfile": "qualification"}}
            if compose
            else {}
        )
        restored = client.Sandbox.create(
            snapshot.snapshot_id,
            idle_timeout=1200,
            schedule_timeout=120,
            init_timeout=180,
            timeout=360,
            **restore_options,
        )
        self.phase("verify original restored process and Nydus bytes")
        self.sandboxes.append(restored.id)
        if compose:
            restored_project = ComposeProject.attach(restored, project.descriptor)
            execute = restored_project.service("left").exec
        else:
            execute = restored.commands.run
        baseline = self.runtime_witness(execute)
        if execute("test ! -e /tmp/native-after-capture").exit_code != 0:
            raise AssertionError("Snapshot includes a post-capture filesystem mutation")

        def resumed():
            after = self.runtime_witness(execute)
            if (
                after["cookie"] != before["cookie"]
                or after["pid"] != before["pid"]
                or after["launches"] != 1
            ):
                raise AssertionError(
                    "checkpoint process was recreated instead of restored"
                )
            return after if after["sequence"] > baseline["sequence"] + 2 else None

        after = wait_for(resumed, seconds=30)
        if execute("sha256sum /image-witness").stdout.split()[0] != expected_hash:
            raise AssertionError("restored Nydus image bytes differ")
        self.observe_protected(image, "environment")
        restored.kill()
        self.sandboxes.remove(restored.id)
        client.Sandbox.delete_snapshot(snapshot.snapshot_id)
        self.snapshots.remove(snapshot.snapshot_id)

        def eligible():
            image._mutate("observe")
            return image.info["eligible_for_reclamation"]

        wait_for(eligible, seconds=30)
        self.results["compose" if compose else "single"] = {
            "publication_id": image.id,
            "snapshot_id": snapshot.snapshot_id,
            "before": before,
            "restored_baseline": baseline,
            "after": after,
            "image_sha256": expected_hash,
            "control_restart": not compose,
            "post_capture_write_absent": True,
        }
        return image

    def verify(self):
        self.phase("build retained Nydus survivor")
        first, first_hash = self.context("single-context")
        # Exercise the default build entry point as well as explicit Client calls.
        connection = self.connection_environment()
        previous = {key: os.environ.get(key) for key in connection}
        try:
            os.environ.update(connection)
            survivor = build_image(
                first,
                profile="qualification",
                retention=ImageRetention(),
                request_id=self.run_id + "-survivor",
                timeout=600,
            ).managed_image
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.publications.append(survivor.id)
        single = self.exercise(first, first_hash)
        second, second_hash = self.context("compose-context")
        compose = self.exercise(second, second_hash, compose=True)
        registry = reclaimer.Registry(
            self.registry_url, allow_http=True, basic_auth=self.registry_auth
        )
        try:
            # Verify the survivors' entire manifest/blob graph after the physical sweep.
            graph = registry.graph(list(survivor.outputs.values()))
            blobs = set()
            nydus = False
            for reference in graph:
                manifest = registry.read(reference)
                for descriptor in [
                    manifest.get("config", {}),
                    *manifest.get("layers", []),
                ]:
                    if "digest" in descriptor:
                        blobs.add((reference.split("@", 1)[0], descriptor["digest"]))
                    nydus |= "nydus" in json.dumps(descriptor)
            if not nydus:
                raise AssertionError("native exporter did not produce Nydus metadata")
            self.results["manifest_deletion"] = [
                self.operator(image) for image in (single, compose)
            ]
            self.phase("no-writer physical Registry GC")
            before_gc = allocated(self.data / "registry")
            command("docker", "stop", self.buildkit)
            command("docker", "stop", self.registry)
            for dry_run in (True, False):
                args = [
                    "docker",
                    "run",
                    "--rm",
                    "--label",
                    "akernel.qualification=" + self.run_id,
                    "-v",
                    f"{self.data / 'registry'}:/var/lib/registry",
                    "-v",
                    f"{self.data / 'registry.yaml'}:/etc/distribution/config.yml:ro",
                    self.args.registry_image,
                    "garbage-collect",
                ]
                if dry_run:
                    args.append("--dry-run")
                output = command(*args, "/etc/distribution/config.yml")
                (self.output / ("gc-dry-run.log" if dry_run else "gc.log")).write_text(
                    output
                )
                if dry_run and allocated(self.data / "registry") != before_gc:
                    raise AssertionError("GC dry-run changed allocated bytes")
            after_gc = allocated(self.data / "registry")
            if after_gc >= before_gc:
                raise AssertionError("GC did not reclaim physical Registry bytes")
            command("docker", "start", self.registry)
            wait_for(
                lambda: (
                    httpx.get(
                        self.registry_url + "/v2/", auth=self.registry_auth, timeout=2
                    ).status_code
                    == 200
                )
            )
            with httpx.Client(timeout=30, auth=self.registry_auth) as http:
                for repository, hash_value in blobs:
                    response = http.get(
                        f"http://{repository.split('/', 1)[0]}/v2/{repository.split('/', 1)[1]}/blobs/{hash_value}"
                    )
                    response.raise_for_status()
                    if digest(response.content) != hash_value:
                        raise AssertionError("retained blob was damaged")
            for reference in graph:
                if registry.read(reference) is None:
                    raise AssertionError("retained Nydus manifest was lost")
            check = self.client.Sandbox(
                image=survivor.reference, cpu=500, memory=1024, schedule_timeout=120
            )
            self.sandboxes.append(check.id)
            if (
                check.commands.run("sha256sum /image-witness").stdout.split()[0]
                != first_hash
            ):
                raise AssertionError("fresh survivor cannot read image after GC")
            check.kill()
            self.sandboxes.remove(check.id)
            self.results.update(
                native_runtime_checkpoint_test=True,
                nydus=True,
                before_gc_bytes=before_gc,
                after_gc_bytes=after_gc,
                physically_reclaimed_bytes=before_gc - after_gc,
                survivor_blobs_verified=len(blobs),
                survivor_publication=survivor.id,
                status="passed",
            )
        finally:
            registry.close()

    def operator(self, image):
        credentials = self.data / "operator-registry-auth.json"
        write(
            credentials,
            {"username": self.registry_auth[0], "password": self.registry_auth[1]},
        )
        environment = {**os.environ, **self.connection_environment()}
        result = subprocess.run(
            [
                sys.executable,
                str(REPO / "builder/managed-docker/reclaim.py"),
                "--image-id",
                image.id,
                "--registry",
                self.registry_url,
                "--repository-prefix",
                "managed",
                "--registry-basic-auth-file",
                str(credentials),
                "--allow-http",
                "--apply",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        return json.loads(result.stdout)

    def connection_environment(self):
        return {
            "AKERNEL_SERVER_ADDRESS": f"https://127.0.0.1:{self.control_port}",
            "AKERNEL_GATEWAY_ADDRESS": f"http://127.0.0.1:{self.data_port}",
            "AKERNEL_TOKEN": (self.data / "token").read_text().strip(),
            "AKERNEL_VERIFY_TLS": "false",
        }

    def cleanup(self):
        errors = []
        if self.client:
            for identity in self.sandboxes:
                try:
                    self.client.Sandbox.delete(sandbox_id=identity)
                except (
                    BackendOperationError,
                    OSError,
                    RuntimeError,
                    subprocess.SubprocessError,
                ) as error:
                    errors.append("sandbox cleanup: " + type(error).__name__)
            for identity in self.snapshots:
                try:
                    self.client.Sandbox.delete_snapshot(identity)
                except (
                    BackendOperationError,
                    OSError,
                    RuntimeError,
                    subprocess.SubprocessError,
                ) as error:
                    errors.append("snapshot cleanup: " + type(error).__name__)
        for resource in reversed(self.containers):
            identity = resource["id"]
            try:
                self.inspect(identity)
                logs = subprocess.run(
                    ["docker", "logs", identity],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                (self.output / (resource["role"] + ".log")).write_text(
                    logs.stdout + logs.stderr
                )
                if (
                    resource["role"] == "node"
                    and self.inspect(identity)["State"]["Running"]
                ):
                    journal = subprocess.run(
                        [
                            "docker",
                            "exec",
                            identity,
                            "journalctl",
                            "--no-pager",
                            "-n",
                            "1500",
                            "-u",
                            "sandboxd",
                            "-u",
                            "adx",
                            "-u",
                            "akernel-managed-images",
                        ],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    (self.output / "node-journal.log").write_text(
                        journal.stdout + journal.stderr
                    )
                command("docker", "stop", "--time", "150", identity)
                command("docker", "rm", identity)
            except (
                BackendOperationError,
                OSError,
                RuntimeError,
                subprocess.SubprocessError,
            ) as error:
                errors.append("container cleanup: " + type(error).__name__)
        if self.network:
            try:
                command("docker", "network", "rm", self.network)
            except (
                BackendOperationError,
                OSError,
                RuntimeError,
                subprocess.SubprocessError,
            ) as error:
                errors.append("network cleanup: " + type(error).__name__)
        if self.mounted:
            try:
                self.check_loop()
                if (
                    command(
                        "findmnt", "-n", "-o", "SOURCE", "--mountpoint", str(self.data)
                    )
                    != self.loop
                ):
                    raise RuntimeError("owned mount identity changed")
                command("umount", str(self.data))
                self.mounted = False
            except (
                BackendOperationError,
                OSError,
                RuntimeError,
                subprocess.SubprocessError,
            ) as error:
                errors.append("filesystem cleanup: " + type(error).__name__)
        if self.loop and not self.mounted:
            try:
                self.check_loop()
                command("losetup", "--detach", self.loop)
                self.loop = None
            except (
                BackendOperationError,
                OSError,
                RuntimeError,
                subprocess.SubprocessError,
            ) as error:
                errors.append("loop cleanup: " + type(error).__name__)
        self.receipt()
        write(
            self.output / "cleanup.json",
            {"errors": errors, "mounted": self.mounted, "loop": self.loop},
        )
        return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in (
        "node-image",
        "tool-image",
        "input-image",
        "registry-image",
        "buildkit-image",
        "disk-uuid",
    ):
        parser.add_argument("--" + option, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Integration-only dependency, excluded from ordinary SDK and adapter tests.
    import bcrypt

    fixture = Fixture(args)
    fixture.password_hash = bcrypt.hashpw(
        fixture.registry_password.encode(), bcrypt.gensalt()
    ).decode()
    try:
        fixture.setup()
        fixture.verify()
    finally:
        errors = fixture.cleanup()
        write(fixture.output / "result.json", fixture.results)
        if errors:
            fixture.results["status"] = "cleanup_failed"
            write(fixture.output / "result.json", fixture.results)
            raise RuntimeError(
                "owned native qualification cleanup incomplete: " + ", ".join(errors)
            )
    print(json.dumps(fixture.results, sort_keys=True))


if __name__ == "__main__":
    main()
