"""Managed Docker runtime using sandboxd's shared ROFS and private upper mounts."""

from __future__ import annotations

import json
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from ._image_plan import ImageMountPlan
from .types import Mount

if TYPE_CHECKING:
    from .sandbox import Sandbox


_PREPARE_RUNTIME = r"""set -eu
for tool in docker dockerd containerd ctr; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "Docker runtime image is missing $tool" >&2
        exit 127
    }
done
ready=true
for tool in bash python3 findmnt mount tar xz realpath usermod; do
    command -v "$tool" >/dev/null 2>&1 || ready=false
done
if [ "$ready" = false ]; then
    if ! command -v apk >/dev/null 2>&1; then
        printf '%s\n' 'Missing Docker support tools; install bash, python3,' \
            'util-linux, coreutils, shadow, tar and xz in the runtime image.' >&2
        exit 127
    fi
    apk add --no-cache bash python3 coreutils util-linux shadow tar xz
fi
docker compose version >/dev/null
"""


@dataclass(frozen=True)
class DockerRuntime:
    """Prepare a private Docker daemon inside a fresh sandbox.

    Pass sandbox_options() to both fresh creation and from_snapshot(). Call
    start() only on fresh creation: snapshot restore retains the actual daemon,
    containers, sockets and bridge processes. The public default_image provides
    Docker, containerd and Compose; fresh startup installs missing Alpine support
    packages. Custom images must provide these tools. The matching helper is
    source-built from builder/image-bridge, or preinstalled by the operator at
    /opt/akernel-image-bridge. No code is downloaded at runtime.
    """

    default_image: ClassVar[str] = "docker.io/library/docker:28.3.3-dind"

    images: ImageMountPlan | None = None
    bridge_binary: Path | None = None
    writable_mb: int = 1024
    writable_backing: str = "file"
    log_dir: Path | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.writable_mb, bool)
            or not isinstance(self.writable_mb, int)
            or self.writable_mb < 1
        ):
            raise ValueError("writable_mb must be a positive integer")
        if self.writable_backing not in {"file", "memory"}:
            raise ValueError("writable_backing must be file or memory")
        if self.bridge_binary is not None and not Path(self.bridge_binary).is_file():
            raise ValueError("bridge_binary must be an existing matching executable")

    def sandbox_options(self) -> dict[str, Any]:
        """Return public SDK mount/config options; also required during restore."""
        if self.images is None:
            return {}
        options: dict[str, Any] = {"mounts": self.images.mounts(Mount)}
        if self.writable_backing == "file":
            options["extra_config"] = {
                "fileBackedTmpfsMounts": [
                    {
                        "target": "/var/lib/akernel-image-snapshots",
                        "sizeMB": self.writable_mb,
                    }
                ]
            }
        return options

    def _log(self, name: str, content: str) -> None:
        if self.log_dir is not None:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            (self.log_dir / name).write_text(content)

    def _bridge(self, sandbox: Sandbox) -> list[str]:
        plan = self.images
        if plan is None:
            return ["--storage-driver=vfs"]
        remote = "/opt/akernel-docker-runtime"
        sandbox.files.make_dir(remote + "/metadata")
        sandbox.files.write(
            remote + "/metadata/plan.json",
            json.dumps(
                {
                    "images": plan.images,
                    "services": plan.services,
                    "aliases": plan.aliases,
                    "automatic": plan.automatic,
                }
            ),
        )
        for name in sorted(
            {b["file"] for image in plan.images for b in image["blobs"]}
        ):
            sandbox.files.copy_from_local(
                str(plan.directory / name), remote + "/metadata/" + name
            )
        if self.bridge_binary is not None:
            sandbox.files.copy_from_local(
                str(self.bridge_binary), "/opt/akernel-image-bridge"
            )
            sandbox.commands.run(
                "chmod 755 /opt/akernel-image-bridge", timeout=20, check=True
            )
        # A packaged module is a wheel/sdist resource, not a Harbor source path.
        sandbox.files.copy_from_local(
            str(Path(__file__).with_name("_image_bridge_guest.py")),
            remote + "/start.py",
        )
        command = shlex.join(
            [
                "python3",
                remote + "/start.py",
                "--root",
                remote,
                "--plan",
                remote + "/metadata/plan.json",
                "--writable-tmpfs-mb",
                str(self.writable_mb),
                "--writable-backing",
                self.writable_backing,
            ]
        )
        try:
            result = sandbox.commands.run(command, timeout=150, check=True)
            self._log("startup.log", result.stdout + result.stderr)
        except Exception:
            try:
                logs = sandbox.commands.run(
                    f"tail -100 {remote}/snapshotter.log {remote}/containerd.log "
                    f"{remote}/image-registration.log",
                    timeout=20,
                    check=True,
                )
                self._log("services.log", logs.stdout + logs.stderr)
            except Exception:
                pass
            raise
        flags = json.loads(result.stdout)
        if not isinstance(flags, list) or not all(isinstance(x, str) for x in flags):
            raise ValueError("Image bridge returned invalid Docker arguments")
        return flags

    def start(self, sandbox: Sandbox, *, network: str = "bridge") -> None:
        """Start bridge, containerd and dockerd once; caller owns sandbox cleanup.

        Never call on a connected/restored sandbox. Fail closed on an existing
        daemon so a retry cannot replace its mounted image/upper state.
        """
        if network not in {"host", "bridge"}:
            raise ValueError("Docker network must be host or bridge")
        sandbox.commands.run("test ! -S /var/run/docker.sock", timeout=20, check=True)
        sandbox.commands.run(
            shlex.join(["sh", "-c", _PREPARE_RUNTIME]), timeout=300, check=True
        )
        flags = self._bridge(sandbox)
        if network == "host":
            flags += ["--iptables=false", "--ip6tables=false", "--bridge=none"]
        else:
            # gVisor supports legacy SNAT, but not all of Docker's netfilter
            # matchers. Keep Docker's bridges/IPAM/DNS and userland port proxies;
            # configure forwarding inside this sandbox rather than rewriting
            # service networks or installing rules on the node.
            script = Path(__file__).with_name("_docker_network_guest.py").read_text()
            sandbox.commands.run(
                shlex.join(["python3", "-c", script, "bootstrap"]),
                timeout=20,
                check=True,
            )
            flags += ["--iptables=false", "--ip6tables=false", "--userland-proxy=true"]
        sandbox.commands.run(
            "PATH=/opt/akernel-docker-runtime/network-bin:"
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin "
            + shlex.join(["dockerd", *flags])
            + " > /var/log/akernel-dockerd.log 2>&1",
            background=True,
        )
        for _ in range(30):
            try:
                sandbox.commands.run("docker info", timeout=10, check=True)
                return
            except Exception:
                time.sleep(2)
        logs = sandbox.commands.run(
            "cat /var/log/akernel-dockerd.log", timeout=10, check=True
        )
        self._log("dockerd.log", logs.stdout + logs.stderr)
        raise RuntimeError(
            "Docker daemon readiness failed; inspect /var/log/akernel-dockerd.log"
        )

    @staticmethod
    def reconnect_network(sandbox: Sandbox, *, network: str = "bridge") -> None:
        """Refresh owned egress rules after a new outer TAP is assigned.

        Call after snapshot restore, before resuming work. This never starts
        dockerd, containers, image helpers or listeners. It preserves inner
        connections; external connections do not gain IP-migration guarantees.
        """
        if network not in {"host", "bridge"}:
            raise ValueError("Docker network must be host or bridge")
        if network == "bridge":
            script = Path(__file__).with_name("_docker_network_guest.py").read_text()
            sandbox.commands.run(
                shlex.join(["python3", "-c", script, "reconnect"]),
                timeout=20,
                check=True,
            )
