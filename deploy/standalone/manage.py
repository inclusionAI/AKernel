#!/usr/bin/env python3
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Build the current checkout and manage a local standalone profile."""

import argparse
from contextlib import contextmanager
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid


NAMES = ("akernel-node", "akernel-traefik")


class Failure(Exception):
    """An actionable diagnostic that contains no credential values."""


class Manager:
    def __init__(self, root=None, environ=None):
        self.root = Path(root or Path(__file__).resolve().parents[2]).resolve()
        self.env = dict(os.environ if environ is None else environ)
        data = Path(self.env.get("AKERNEL_STANDALONE_DATA_DIR", str(self.root / ".akernel/standalone/data")))
        if not data.is_absolute():
            raise Failure("AKERNEL_STANDALONE_DATA_DIR must be absolute")
        self.data = data.resolve()
        self.directory = self.root / "deploy/standalone"
        self.state_file = self.data / "source-state.json"
        self.state = {}

    @contextmanager
    def lock(self):
        self.data.parent.mkdir(parents=True, exist_ok=True)
        path = self.data.parent / f".{self.data.name}.source.lock"
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Failure("another operation is using this standalone profile") from None
            yield
        finally:
            os.close(descriptor)

    def capture(self, *args):
        result = subprocess.run(args, cwd=self.root, env=self.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            raise Failure(f"{args[0]} {args[1] if len(args) > 1 else ''} failed; check local prerequisites")
        return result.stdout.strip()

    def run(self, *args):
        if subprocess.call(args, cwd=self.root, env=self.env):
            raise Failure(f"{Path(args[0]).name} failed; profile data and images were retained")

    def inventory(self):
        output = self.capture("docker", "ps", "-a", "--no-trunc", "--format", "{{json .}}")
        containers = {}
        for line in output.splitlines():
            row = json.loads(line)
            if row["Names"] in NAMES:
                if row["Names"] in containers or not re.fullmatch(r"[0-9a-f]{64}", row["ID"]):
                    raise Failure("invalid standalone container inventory")
                containers[row["Names"]] = row["ID"]
        return containers

    def native_arch(self):
        # All profile mounts and resolver paths belong to this machine.
        if self.env.get("DOCKER_CONTEXT") or not self.env.get("DOCKER_HOST"):
            context = self.capture("docker", "context", "show")
            endpoint = json.loads(self.capture("docker", "context", "inspect", context))[0]["Endpoints"]["docker"]["Host"]
        else:
            endpoint = self.env["DOCKER_HOST"]
        if not endpoint.startswith("unix://"):
            raise Failure("source standalone requires a local Docker socket, not a remote context")
        info = json.loads(self.capture("docker", "info", "--format", "{{json .}}"))
        arch = {"amd64": "amd64", "x86_64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(info["Architecture"])
        if info["OSType"] != "linux" or arch is None:
            raise Failure("Docker must provide native Linux amd64 or arm64")
        if platform.system() == "Darwin" and (info["OperatingSystem"] != "OrbStack" or arch != "arm64"):
            raise Failure("macOS source standalone requires OrbStack ARM64")
        if platform.system() not in ("Darwin", "Linux"):
            raise Failure("source standalone supports Linux and Apple Silicon with OrbStack")
        explicit = self.env.get("AKERNEL_TARGETARCH", arch)
        explicit = {"x86_64": "amd64", "aarch64": "arm64"}.get(explicit, explicit)
        if explicit != arch:
            raise Failure("AKERNEL_TARGETARCH must match the local Docker engine")
        return arch

    def prepare_source(self):
        expected = self.capture("git", "ls-tree", "HEAD", "src/sandboxd").split()
        if len(expected) != 4 or expected[:2] != ["160000", "commit"]:
            raise Failure("src/sandboxd must be a recorded Git submodule")
        component = self.root / "src/sandboxd"
        if not (component / ".git").exists():
            if component.exists() and any(component.iterdir()):
                raise Failure("uninitialized src/sandboxd contains files; preserve them before initialization")
            self.run("git", "submodule", "update", "--init", "--", "src/sandboxd")
        actual = self.capture("git", "-C", str(component), "rev-parse", "HEAD")
        if actual != expected[2] or self.capture("git", "-C", str(component), "status", "--porcelain=v1"):
            raise Failure("src/sandboxd is dirty or differs from the gitlink; align it explicitly before building")
        revision = self.capture("git", "rev-parse", "HEAD")
        if self.capture("git", "status", "--porcelain=v1"):
            revision += ".dirty"
        return revision, actual

    def configure(self, arch):
        for name in ("RUNC", "KATA", "FIRECRACKER", "GPU"):
            key = f"AKERNEL_ENABLE_{name}"
            self.env.setdefault(key, "false")
            if self.env[key] not in ("true", "false"):
                raise Failure(f"{key} must be true or false")
        if arch == "arm64" and any(self.env[f"AKERNEL_ENABLE_{name}"] != "false" for name in ("KATA", "FIRECRACKER", "GPU")):
            raise Failure("ARM64 requires Kata, Firecracker and GPU disabled")
        if self.env.get("RUNTIME_PROFILE", "rrt") != "rrt":
            raise Failure("source standalone uses RUNTIME_PROFILE=rrt; use make build for other profiles")
        # Keep pulls and launches native as well as the source image build.
        self.env.update(AKERNEL_TARGETARCH=arch, DOCKER_DEFAULT_PLATFORM=f"linux/{arch}",
                        AKERNEL_STANDALONE_DATA_DIR=str(self.data))
        if self.env["AKERNEL_ENABLE_RUNC"] == "true":
            spec = importlib.util.spec_from_file_location("standalone_resolver", self.directory / "select-resolver.py")
            resolver = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(resolver)
            source = self.env.get("AKERNEL_RUNC_RESOLV_CONF")
            try:
                resolver.select_resolver(Path(source) if source else None)
            except (OSError, UnicodeError, ValueError) as exc:
                raise Failure(str(exc)) from None

    def save(self):
        self.data.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=self.data, delete=False) as stream:
            json.dump(self.state, stream, indent=2)
            stream.write("\n")
            temporary = Path(stream.name)
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.state_file)

    def read_state(self):
        if not self.state_file.is_file():
            raise Failure(f"no source profile at {self.data}; use make standalone to create one")
        self.state = json.loads(self.state_file.read_text())
        if self.state.get("data_dir") != str(self.data):
            raise Failure("profile metadata belongs to another data directory")

    def inspect_owned(self, containers):
        inspected = {}
        for name, container_id in containers.items():
            expected = self.state.get("containers", {}).get(name)
            if expected and expected != container_id:
                raise Failure(f"{name} was replaced; refusing to manage another container")
            obj = json.loads(self.capture("docker", "inspect", container_id))[0]
            source, destination = (str(self.data), "/home/akernel") if name == NAMES[0] else (str(self.data / "traefik/dynamic.yml"), "/etc/traefik/dynamic.yml")
            if not any(m.get("Type") == "bind" and m.get("Source") == source and m.get("Destination") == destination for m in obj["Mounts"]):
                raise Failure(f"{name} belongs to another data directory")
            if name == NAMES[0] and obj["Image"] != self.state.get("image_id"):
                raise Failure("node image does not match this source profile")
            inspected[name] = obj
        return inspected

    def sdk_environment(self, address):
        script = "# Local standalone: read credentials at use time, without printing them.\nset +x\n"
        script += "export AKERNEL_BACKEND=openyuanrong-sandbox\n"
        script += f"export AKERNEL_SERVER_ADDRESS={shlex.quote(address)}\n"
        script += f"export AKERNEL_TOKEN=\"$(cat {shlex.quote(str(self.data / 'token'))})\"\n"
        script += 'export NO_PROXY="${NO_PROXY:+${NO_PROXY},}${AKERNEL_SERVER_ADDRESS}"\n'
        script += 'export no_proxy="${no_proxy:+${no_proxy},}${AKERNEL_SERVER_ADDRESS}"\n'
        path = self.data / "sdk-env.sh"
        path.write_text(script)
        path.chmod(0o600)
        print(f"SDK environment: source {shlex.quote(str(path))}")

    def up(self):
        with self.lock():
            self._up()

    def _up(self):
        for command in ("git", "docker", "bash", "curl"):
            if shutil.which(command, path=self.env.get("PATH")) is None:
                raise Failure(f"{command} is required")
        arch = self.native_arch()
        if self.inventory():
            raise Failure("standalone container names are already occupied; drain and stop that profile first")
        self.configure(arch)
        revision, component = self.prepare_source()
        image = f"akernel-local/all-in-one:{revision[:12]}-{arch}-{uuid.uuid4().hex[:8]}"
        self.state = {"data_dir": str(self.data), "revision": revision, "sandboxd_revision": component,
                      "platform": f"linux/{arch}", "image": image, "status": "building", "containers": {}}
        self.save()
        try:
            # Do not load the default cloud deployment profile.
            self.run("bash", str(self.root / "deploy/scripts/build-image.sh"), "--repository", image.rsplit(":", 1)[0], "--tag", image.rsplit(":", 1)[1], "--runtime-profile", "rrt")
            obj = json.loads(self.capture("docker", "image", "inspect", image))[0]
            if f"{obj['Os']}/{obj['Architecture']}" != f"linux/{arch}" or obj["Config"]["Labels"].get("org.opencontainers.image.revision") != revision:
                raise Failure("built image platform or source revision does not match")
            self.state.update(image_id=obj["Id"], status="starting")
            self.save()
            self.env["IMAGE"] = image
            self.run("bash", str(self.directory / "start.sh"))
            containers = self.inventory()
            owned = self.inspect_owned(containers)
            if set(owned) != set(NAMES) or not all(obj["State"]["Running"] for obj in owned.values()):
                raise Failure("standalone did not start both containers")
            self.state.update(containers=containers, status="ready")
            networks = owned[NAMES[1]]["NetworkSettings"]["Networks"]
            address = next(iter(networks.values()))["IPAddress"]
            if not address:
                raise Failure("gateway has no reachable address")
            self.state["address"] = address
            self.save()
            self.sdk_environment(address)
        except (Failure, OSError, ValueError, KeyError, IndexError, TypeError):
            self.state["status"] = "failed"
            self.save()
            print(f"Inspect the profile with make standalone-status; after draining use make standalone-stop. Data: {self.data}", file=sys.stderr)
            raise

    def status(self):
        with self.lock():
            self._status()

    def _status(self):
        self.read_state()
        owned = self.inspect_owned(self.inventory())
        print(f"Profile: {self.data}\nSource: {self.state['revision']}\nImage: {self.state['image']}\nLast operation: {self.state['status']}\nToken file: {self.data / 'token'}")
        for name in NAMES:
            print(f"{name}: {'running' if owned.get(name, {}).get('State', {}).get('Running') else 'stopped or absent'}")
        address = self.state.get("address")
        if set(owned) == set(NAMES) and all(obj["State"]["Running"] for obj in owned.values()) and address:
            self.capture("curl", "--noproxy", "*", "-fkSs", "--max-time", "5", f"https://{address}/healthz")
            print(f"Gateway: https://{address} (healthy)\nSDK environment: {self.data / 'sdk-env.sh'}")
        elif owned:
            raise Failure("standalone is not fully running; inspect Docker logs")

    def stop(self):
        with self.lock():
            self._stop()

    def _stop(self):
        self.read_state()
        containers = self.inventory()
        owned = self.inspect_owned(containers)
        node = owned.get(NAMES[0])
        if node and node["State"]["Running"]:
            listing = self.capture("docker", "exec", containers[NAMES[0]], "sbox", "--address", "/run/sandboxd/sandboxd.sock", "list").splitlines()
            if len(listing) != 1 or listing[0].split() != ["ID", "STATUS", "RUNTIME", "AGE", "CREATED"]:
                raise Failure("release all sandboxes before stopping; if sandboxd is unavailable, inspect the node manually")
        for name, key in ((NAMES[0], "AKERNEL_EXPECTED_NODE_ID"), (NAMES[1], "AKERNEL_EXPECTED_TRAEFIK_ID")):
            self.env[key] = containers.get(name, "absent")
        self.run("bash", str(self.directory / "stop.sh"))
        self.state["status"] = "stopped"
        self.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("up", "status", "stop"))
    args = parser.parse_args()
    try:
        getattr(Manager(), args.command)()
    except (Failure, OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        parser.exit(1, f"standalone: {exc}\n")


if __name__ == "__main__":
    main()
