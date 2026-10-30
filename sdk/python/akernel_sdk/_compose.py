"""Compose workloads inside a Sandbox; independent of any evaluation harness."""

from __future__ import annotations

import ipaddress
import json
import re
import shlex
import shutil
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from uuid import uuid4

import yaml

from ._backends.errors import (
    BackendOperationError,
    TimeoutException,
    retain_failure_context,
)
from ._docker_runtime import DockerRuntime
from ._image_plan import ImageMountPlan
from ._image_resolver import prepare_compose_images
from .types import CommandResult, HttpReverseTunnel, Mount

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ._service_process import ServiceProcessHandle
    from .sandbox import Sandbox


class ComposeOperationError(BackendOperationError):
    """Compose returned a failure, retaining phase and command output."""

    def __init__(self, phase: str, result: CommandResult) -> None:
        super().__init__(
            f"Compose {phase} failed (exit_code={result.exit_code})",
            code="COMPOSE_FAILED",
            outcome="unknown" if result.exit_code is None else "terminal",
        )
        self.phase = phase
        self.result = result
        self.stdout, self.stderr = result.stdout, result.stderr
        self.exit_code = result.exit_code


def _services(contents: Sequence[str]) -> dict[str, Any]:
    services: dict[str, Any] = {}
    for content in contents:
        document = yaml.safe_load(content) or {}
        for name, config in document.get("services", {}).items():
            services.setdefault(name, {}).update(config or {})
    if not services:
        raise ValueError("Compose project requires at least one service")
    return services


def host_network_overlay(
    services: Mapping[str, Any], *, prebuilt: Sequence[str] = ()
) -> dict[str, Any]:
    """Explicit runsc host-network compatibility profile, preserving task files."""
    if any(config.get("networks") for config in services.values()):
        raise ValueError("Host-network Compose does not support explicit networks")
    hostnames = {
        name: config.get("hostname") or name for name, config in services.items()
    }
    aliases = list(dict.fromkeys([*services, *hostnames.values()]))
    return {
        "services": {
            name: {
                "network_mode": "host",
                "hostname": hostnames[name],
                "extra_hosts": [f"{alias}:127.0.0.1" for alias in aliases],
                **(
                    {"build": {"network": "host"}}
                    if config.get("build") is not None and name not in prebuilt
                    else {}
                ),
            }
            for name, config in services.items()
        }
    }


class ComposeProject:
    """Prepared client descriptor and remote Compose operations.

    prepare() freezes the project context and Compose bytes before resolving
    image inputs. No server is changed until start_runtime(). Restore with
    attach(): it performs no upload, build, up, or daemon startup. The caller
    owns the outer Sandbox and its deletion after a partial startup failure.
    """

    _descriptor: dict[str, Any]
    _runtime: DockerRuntime | None
    _context: Path | None
    _output: Path | None
    _contents: list[str]
    _sandbox: Sandbox | None
    _startup_attempted: bool

    @classmethod
    def prepare(
        cls,
        directory: str | Path,
        files: Sequence[str | Path] = (),
        *,
        project_name: str = "akernel",
        base: Mapping[str, Any] | None = None,
        overrides: Sequence[Mapping[str, Any]] = (),
        runtime: DockerRuntime | None = None,
        auto_images: bool = False,
        output_dir: str | Path | None = None,
        environment: Mapping[str, str] | None = None,
        network: str = "bridge",
        buildkit: bool = True,
        prebuilt_images: Mapping[str, str] | None = None,
        remote_directory: str = "/opt/akernel-compose/project",
        directories: Mapping[str, int] | None = None,
    ) -> ComposeProject:
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project_name) is None:
            raise ValueError("Invalid Compose project_name")
        if auto_images and not buildkit:
            raise ValueError("Automatic images require BuildKit")
        if network not in {"host", "bridge"}:
            raise ValueError("Compose network must be host or bridge")
        if not remote_directory.startswith("/") or ".." in Path(remote_directory).parts:
            raise ValueError("remote_directory must be an absolute normalized path")
        for path, mode in (directories or {}).items():
            if (
                not path.startswith("/")
                or ".." in Path(path).parts
                or isinstance(mode, bool)
                or not isinstance(mode, int)
                or not 0 <= mode <= 0o777
            ):
                raise ValueError("Invalid bind directory initialization")
        source = Path(directory).resolve(strict=True)
        output = (
            Path(output_dir).resolve()
            if output_dir
            else Path(tempfile.mkdtemp(prefix="akernel-compose-"))
        )
        if output == source or source in output.parents:
            raise ValueError("Compose output_dir must be outside the source context")
        output.mkdir(parents=True, exist_ok=True, mode=0o700)
        context = output / "context"
        if context.exists():
            raise ValueError("Compose output_dir already contains a prepared context")
        contents: list[str] = []
        names: list[str] = []
        if base is not None:
            names.append("base.json")
            contents.append(json.dumps(base))
        for index, file in enumerate(files):
            names.append(f"source-{index}.yaml")
            contents.append(Path(file).read_text())
        for index, override in enumerate(overrides):
            names.append(f"override-{index}.json")
            contents.append(json.dumps(override))
        services = _services(contents)
        # Preserve original definitions; compatibility policy is an extra file.
        selected = set(prebuilt_images or {}) & services.keys()
        if network == "host":
            names.append("host.json")
            contents.append(
                json.dumps(host_network_overlay(services, prebuilt=sorted(selected)))
            )
        # A private local copy makes discovery and upload see the same context.
        shutil.copytree(source, context, symlinks=True)
        local_files = []
        for name, content in zip(names, contents, strict=True):
            local_path = output / name
            local_path.write_text(content)
            local_files.append(local_path)
        runtime = runtime or DockerRuntime()
        if auto_images:
            if runtime.bridge_binary is None:
                raise ValueError("Automatic images require a matching bridge binary")
            images = prepare_compose_images(
                context,
                local_files,
                output_dir=output / "images",
                bridge_binary=runtime.bridge_binary,
                env=environment,
            )
            runtime = replace(runtime, images=images)
        if selected:
            lines = ["services:"]
            for name in sorted(selected):
                lines += [
                    f"  {json.dumps(name)}:",
                    "    build: !reset null",
                    f"    image: {json.dumps((prebuilt_images or {})[name])}",
                ]
            names.append("prebuilt.yaml")
            contents.append("\n".join(lines) + "\n")
        result = cls.__new__(cls)
        result._runtime = runtime
        result._context = context
        result._contents = contents
        result._output = output
        result._sandbox = None
        result._startup_attempted = False
        result._descriptor = {
            "schema_version": 1,
            "project_name": project_name,
            "directory": remote_directory,
            "files": [remote_directory + ".config/" + n for n in names],
            "services": list(services),
            "network": network,
            "buildkit": buildkit,
            "prebuilt_only": runtime.images is not None and not auto_images,
            "automatic_images": auto_images,
            "environment": dict(environment or {}),
            "directories": dict(directories or {}),
        }
        options = runtime.sandbox_options()
        result._descriptor["sandbox_options"] = {
            **options,
            "mounts": [
                {"target": m.target, "image_url": m.image_url}
                for m in options.get("mounts", [])
            ],
        }
        return result

    @classmethod
    def attach(cls, sandbox: Sandbox, descriptor: Mapping[str, Any]) -> ComposeProject:
        """Rebind captured remote state only, with no fresh-boot operations."""
        data = json.loads(json.dumps(descriptor, allow_nan=False))
        if data.get("schema_version") != 1 or not isinstance(
            data.get("services"), list
        ):
            raise ValueError("Invalid Compose project descriptor")
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", data.get("project_name", "")) is None:
            raise ValueError("Invalid Compose project name")
        paths = [data.get("directory"), *data.get("files", [])]
        if not all(
            isinstance(p, str) and p.startswith("/") and ".." not in Path(p).parts
            for p in paths
        ):
            raise ValueError("Invalid Compose project paths")
        result = cls.__new__(cls)
        result._descriptor = data
        result._sandbox = sandbox
        result._startup_attempted = True
        result._output = None
        result._runtime = None
        result._context = None
        result._contents = []
        return result

    @property
    def descriptor(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._descriptor))

    @property
    def images(self) -> ImageMountPlan | None:
        return self._runtime.images if self._runtime else None

    def sandbox_options(self) -> dict[str, Any]:
        options = json.loads(json.dumps(self._descriptor["sandbox_options"]))
        options["mounts"] = [Mount(**value) for value in options.get("mounts", [])]
        return options

    def _require_sandbox(self) -> Sandbox:
        if self._sandbox is None:
            raise RuntimeError("Compose project is not bound to a Sandbox")
        return self._sandbox

    def start_runtime(self, sandbox: Sandbox) -> None:
        if (
            self._sandbox is not None
            or self._runtime is None
            or self._startup_attempted
        ):
            raise RuntimeError("Compose runtime startup is fresh-only")
        self._startup_attempted = True
        self._runtime.start(sandbox, network=self._descriptor["network"])
        self._sandbox = sandbox
        directory = self._descriptor["directory"]
        sandbox.files.copy_from_local(str(self._context), directory)
        sandbox.files.make_dir(directory + ".config")
        for remote, content in zip(
            self._descriptor["files"], self._contents, strict=True
        ):
            sandbox.files.write(remote, content)
        for path, mode in self._descriptor["directories"].items():
            sandbox.commands.run(
                shlex.join(["install", "-d", "-m", oct(mode)[2:], path]),
                timeout=20,
                check=True,
            )

    def reconnect_network(self) -> None:
        """Rebind managed egress rules after restoring to a new outer TAP.

        Call after attach and before resuming work. Does not start a daemon,
        build/up a project, replace containers or recreate tunnel listeners.
        Only SDK-owned IPv4 forwarding/NAT chains can change. Inner network
        identities and existing connections inside the sandbox remain intact;
        connections to external peers do not gain IP-migration guarantees.
        """
        DockerRuntime.reconnect_network(
            self._require_sandbox(), network=self._descriptor["network"]
        )

    def command(self, args: Sequence[str]) -> str:
        if not args:
            raise ValueError("Compose subcommand is required")
        parts = [
            "docker",
            "compose",
            "-p",
            self._descriptor["project_name"],
            "--project-directory",
            self._descriptor["directory"],
        ]
        for file in self._descriptor["files"]:
            parts += ["-f", file]
        prefix = (
            f"DOCKER_BUILDKIT={int(self._descriptor['buildkit'])} "
            if args[0] == "build"
            else ""
        )
        # Interpolation gets only explicit project variables, never host secrets.
        if self._descriptor["environment"]:
            prefix += (
                "env "
                + shlex.join(
                    [f"{k}={v}" for k, v in self._descriptor["environment"].items()]
                )
                + " "
            )
        return prefix + shlex.join([*parts, *args])

    def run(
        self,
        args: Sequence[str],
        *,
        timeout: int = 60,
        check: bool = False,
        cancellation: Event | None = None,
    ) -> CommandResult:
        result = self._execute(
            self.command(args), timeout=timeout, cancellation=cancellation
        )
        if check and result.exit_code != 0:
            raise ComposeOperationError(args[0], result)
        return result

    def _execute(
        self, command: str, *, timeout: int, cancellation: Event | None = None
    ) -> CommandResult:
        sandbox = self._require_sandbox()
        if cancellation is None and timeout != 0:
            result = sandbox.commands.run(command, cwd="/", timeout=timeout)
        else:
            # A zero execution budget means no deadline. Waits remain bounded
            # so an async caller can cancel without abandoning remote work.
            handle = sandbox.commands.run(command, cwd="/", background=True)
            deadline = time.monotonic() + timeout if timeout > 0 else None
            while True:
                cancelled = cancellation is not None and cancellation.is_set()
                expired = deadline is not None and time.monotonic() >= deadline
                if cancelled or expired:
                    failure = TimeoutException(
                        "Compose command cancelled"
                        if cancelled
                        else "Compose command timed out",
                        command_id=handle.command_id,
                        outcome="unknown",
                    )
                    try:
                        handle.kill()
                    except BaseException as cleanup_error:
                        retain_failure_context(failure, sandbox.id, cleanup_error)
                    raise failure
                result = handle.wait(timeout=1)
                if result.exit_code is not None:
                    break
        return result

    def _operation(self, args: Sequence[str], timeout: int) -> CommandResult:
        try:
            result = self.run(args, timeout=timeout)
        except BackendOperationError:
            # Unknown outcomes are never retried by the Compose layer.
            raise
        if self._output is not None:
            (self._output / (args[0] + ".log")).write_text(
                result.stdout + result.stderr
            )
        if result.exit_code != 0:
            if args[0] == "up":
                try:
                    logs = self.run(["logs", "--no-color", "--tail", "200"], timeout=30)
                    if self._output is not None:
                        (self._output / "services.log").write_text(
                            logs.stdout + logs.stderr
                        )
                except Exception:
                    pass
            raise ComposeOperationError(args[0], result)
        return result

    def build(self, *, no_cache: bool = False, timeout: int = 600) -> None:
        if not self._descriptor["prebuilt_only"]:
            self._operation(["build", *(["--no-cache"] if no_cache else [])], timeout)

    def pull(self, *, timeout: int = 600) -> None:
        """Prepare service images, verifying existing ROFS inputs in auto mode.

        Automatic discovery already resolved and mounted all external inputs.
        Native Compose pull can still unpack digest references despite its
        missing policy, so use the checked mounted images rather than pull again.
        """
        if self._descriptor["automatic_images"]:
            self._verify_images("pull", timeout)
        elif not self._descriptor["prebuilt_only"]:
            self._operation(["pull", "--ignore-buildable"], timeout)

    def _verify_images(self, phase: str, timeout: int) -> None:
        result = self._execute(
            "python3 /opt/akernel-docker-runtime/start.py --verify "
            "--plan /opt/akernel-docker-runtime/metadata/plan.json",
            timeout=timeout,
        )
        if self._output is not None:
            (self._output / (phase + "-images.log")).write_text(
                result.stdout + result.stderr
            )
            if phase == "pull":
                (self._output / "pull.log").write_text(result.stdout + result.stderr)
        if result.exit_code != 0:
            raise ComposeOperationError(phase, result)

    def up(self, *, wait_timeout: int = 600, timeout: int = 600) -> None:
        if self._descriptor["automatic_images"]:
            self._verify_images("up", timeout)
        self._operation(
            [
                "up",
                "-d",
                "--wait",
                "--wait-timeout",
                str(wait_timeout),
                *(
                    ["--no-build", "--pull", "never"]
                    if self._descriptor["prebuilt_only"]
                    else ["--pull", "never"]
                    if self._descriptor["automatic_images"]
                    else []
                ),
            ],
            timeout,
        )

    def ps(self) -> list[dict[str, Any]]:
        output = self.run(["ps", "-a", "--format", "json"], check=True).stdout
        rows = []
        for line in output.splitlines():
            parsed = json.loads(line)
            rows.extend(parsed if isinstance(parsed, list) else [parsed])
        return rows

    def logs(self, *, tail: int = 200) -> str:
        return self.run(["logs", "--no-color", "--tail", str(tail)], check=True).stdout

    def down(self, *, timeout: int = 60) -> None:
        self.run(["down"], timeout=timeout, check=True)

    def service(self, name: str) -> ServiceHandle:
        if name not in self._descriptor["services"]:
            raise ValueError(f"Unknown Compose service: {name}")
        return ServiceHandle(self, name)


class ServiceHandle:
    """Operations in one Compose service; no hardcoded main or log paths."""

    def __init__(self, project: ComposeProject, name: str) -> None:
        self.project, self.name = project, name

    def identity(self) -> dict[str, str]:
        ids = self.project.run(["ps", "-a", "-q", self.name], check=True).stdout.split()
        if len(ids) != 1:
            raise BackendOperationError(
                "Service must have exactly one container", code="SERVICE_UNAVAILABLE"
            )
        result = self.project._require_sandbox().commands.run(
            shlex.join(["docker", "inspect", ids[0]]), timeout=20, check=True
        )
        data = json.loads(result.stdout)[0]
        return {"container_id": data["Id"], "started_at": data["State"]["StartedAt"]}

    def expose_tunnel(self, tunnel: HttpReverseTunnel) -> str:
        """Return this service's reachable URL for an outer reverse tunnel.

        Host-network services use the existing loopback listener. For bridge
        networks, a private TCP relay binds only to an attached gateway and
        accepts only this service's IP. HTTP, credentials and SSE pass through
        unchanged. The daemon and descriptor survive whole-sandbox snapshots;
        attach never restarts a relay. Requires IPv4 on one attached network.
        An ambiguous launch is never retried automatically.
        """
        if not isinstance(tunnel, HttpReverseTunnel):
            raise TypeError("tunnel must be an HttpReverseTunnel")
        identity = self.identity()
        key = f"{self.name}:{tunnel.listen_port}"
        endpoints = self.project._descriptor.setdefault("tunnel_endpoints", {})
        if key in endpoints:
            record = endpoints[key]
            if any(record[k] != identity[k] for k in identity):
                raise BackendOperationError(
                    "Tunnel service was replaced or restarted",
                    code="SERVICE_TUNNEL_STALE",
                    outcome="not_started",
                )
            if "url" not in record:
                raise BackendOperationError(
                    "Tunnel launch outcome unknown; do not resubmit",
                    code="SERVICE_TUNNEL_UNKNOWN",
                    outcome="unknown",
                )
            return str(record["url"])
        if self.project._runtime is None:
            raise RuntimeError("Restored projects can only attach existing tunnels")
        sandbox = self.project._require_sandbox()
        result = sandbox.commands.run(
            shlex.join(["docker", "inspect", identity["container_id"]]),
            timeout=20,
            check=True,
        )
        data = json.loads(result.stdout)[0]
        if data["HostConfig"]["NetworkMode"] == "host":
            endpoints[key] = {**identity, "url": tunnel.url}
            return tunnel.url
        networks = data["NetworkSettings"].get("Networks") or {}
        connected = []
        for name, value in sorted(networks.items()):
            if not value.get("IPAddress"):
                continue
            gateway = value.get("Gateway")
            if not gateway:
                # Internal Docker networks intentionally omit a default route;
                # their local bridge gateway can still host a controlled tunnel.
                inspect = sandbox.commands.run(
                    shlex.join(["docker", "network", "inspect", name]),
                    timeout=20,
                    check=True,
                )
                config = json.loads(inspect.stdout)[0]["IPAM"]["Config"]
                gateway = next(
                    (
                        v["Gateway"]
                        for v in config
                        if v.get("Gateway") and ":" not in v["Gateway"]
                    ),
                    None,
                )
            if gateway:
                connected.append({"Gateway": gateway, "IPAddress": value["IPAddress"]})
        if not connected:
            raise ValueError("Service tunnel requires an attached IPv4 bridge network")
        network = connected[0]
        address = str(ipaddress.IPv4Address(network["Gateway"]))
        peer = str(ipaddress.IPv4Address(network["IPAddress"]))
        if (
            ipaddress.IPv4Address(address).is_unspecified
            or ipaddress.IPv4Address(address).is_loopback
        ):
            raise ValueError("Service tunnel requires a non-loopback bridge gateway")
        operation_id = uuid4().hex
        # Reserve before dispatch so a lost reply cannot create a second relay.
        record = {**identity, "operation_id": operation_id}
        endpoints[key] = record
        script = Path(__file__).with_name("_docker_network_guest.py").read_text()
        result = sandbox.commands.run(
            shlex.join(
                [
                    "python3",
                    "-c",
                    script,
                    "expose",
                    "/opt/akernel-docker-runtime/tunnels/" + operation_id,
                    json.dumps(
                        {
                            "address": address,
                            "allowed": [peer],
                            "target_port": tunnel.listen_port,
                        }
                    ),
                ]
            ),
            timeout=20,
            check=True,
        )
        value = json.loads(result.stdout)
        url = urlsplit(value["url"])
        if (
            url.scheme != "http"
            or url.hostname != address
            or not url.port
            or url.username
            or url.path
            or url.query
            or url.fragment
        ):
            raise ValueError("Runtime returned an invalid service tunnel endpoint")
        record["url"] = value["url"]
        return str(record["url"])

    def _args(
        self,
        command: str,
        *,
        cwd: str | None,
        envs: Mapping[str, str] | None,
        user: str | int | None,
        shell: str,
        login: bool = False,
    ) -> list[str]:
        args = ["exec", "-T"]
        if cwd:
            args += ["-w", cwd]
        if user is not None:
            args += ["-u", str(user)]
        for key, value in (envs or {}).items():
            args += ["-e", f"{key}={value}"]
        return [*args, self.name, shell, "-lc" if login else "-c", command]

    def exec(
        self,
        command: str,
        *,
        cwd: str | None = None,
        envs: Mapping[str, str] | None = None,
        user: str | int | None = None,
        shell: str = "/bin/sh",
        login: bool = False,
        timeout: int = 60,
        cancellation: Event | None = None,
    ) -> CommandResult:
        return self.project.run(
            self._args(
                command, cwd=cwd, envs=envs, user=user, shell=shell, login=login
            ),
            timeout=timeout,
            cancellation=cancellation,
        )

    def _copy(self, source: str, target: str, timeout: int) -> None:
        self.project.run(["cp", source, target], timeout=timeout, check=True)

    def upload(
        self,
        source: str | Path,
        target: str,
        *,
        directory: bool = False,
        timeout: int = 120,
    ) -> None:
        sandbox = self.project._require_sandbox()
        temp = "/tmp/akernel-compose-" + uuid4().hex
        failure: BaseException | None = None
        try:
            sandbox.files.copy_from_local(str(source), temp)
            self._copy(
                temp + ("/." if directory else ""), f"{self.name}:{target}", timeout
            )
        except BaseException as error:
            failure = error
            raise
        finally:
            try:
                sandbox.files.remove(temp)
            except Exception as cleanup_error:
                if failure is None:
                    raise
                retain_failure_context(failure, sandbox.id, cleanup_error)

    def download(
        self,
        source: str,
        target: str | Path,
        *,
        directory: bool = False,
        timeout: int = 120,
    ) -> None:
        sandbox = self.project._require_sandbox()
        temp = "/tmp/akernel-compose-" + uuid4().hex
        failure: BaseException | None = None
        try:
            if directory:
                sandbox.files.make_dir(temp)
            self._copy(
                f"{self.name}:{source}" + ("/." if directory else ""), temp, timeout
            )
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            sandbox.files.copy_to_local(temp, str(target))
        except BaseException as error:
            failure = error
            raise
        finally:
            try:
                sandbox.files.remove(temp)
            except Exception as cleanup_error:
                if failure is None:
                    raise
                retain_failure_context(failure, sandbox.id, cleanup_error)

    def stop(self, *, timeout: int = 60) -> None:
        self.project.run(["stop", self.name], timeout=timeout, check=True)

    def start_process(
        self,
        command: str,
        *,
        cwd: str | None = None,
        envs: Mapping[str, str] | None = None,
        user: str | int | None = None,
        shell: str = "/bin/bash",
    ) -> ServiceProcessHandle:
        from ._service_process import ServiceProcessHandle

        return ServiceProcessHandle.start(
            self, command, cwd=cwd, envs=envs, user=user, shell=shell
        )
