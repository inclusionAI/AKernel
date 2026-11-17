"""Start a private containerd and snapshotter for sandboxd-mounted images."""

import json
import subprocess
import time
from pathlib import Path


def verify_image_bridge(plan):
    """Check pinned image records and mounted roots without pulling or unpacking.

    Docker's digest pull path does not treat metadata-only registration as a
    complete local content store. The committed snapshot is our filesystem
    source; missing aliases or snapshots must fail instead of falling back.
    """
    data = json.loads(plan.read_text())
    references = {}
    roots_by_chain: dict[str, set[str]] = {}
    for image in data["images"]:
        references[image["reference"]] = image["target"]["digest"]
        roots_by_chain.setdefault(image["chain_id"], set()).add(image["root"])
    for chain, roots in roots_by_chain.items():
        result = subprocess.run(
            [
                "ctr",
                "--address",
                "/run/akernel-image-bridge/containerd.sock",
                "--namespace",
                "moby",
                "snapshots",
                "--snapshotter",
                "akernel",
                "info",
                chain,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        info = json.loads(result.stdout)
        if (
            info.get("Kind") != "Committed"
            or info.get("Labels", {}).get(
                "containerd.io/snapshot/agent-service.image-root"
            )
            not in roots
        ):
            raise RuntimeError("Prepared image snapshot does not match its ROFS mount")
    for alias, reference in data.get("aliases", {}).items():
        references[alias] = references[reference]
    for reference, expected in references.items():
        result = subprocess.run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Descriptor.Digest}}",
                reference,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.stdout.strip() != expected:
            raise RuntimeError("Prepared image reference changed: " + reference)
    print("Verified pinned ROFS images; no layer pull or extraction required")


def start_image_bridge(
    root, plan, children, writable_tmpfs_mb=0, writable_backing="memory"
):
    if writable_tmpfs_mb < 0:
        raise ValueError("writable tmpfs size must be nonnegative")
    if writable_backing not in {"memory", "file"}:
        raise ValueError("writable backing must be memory or file")
    snapshots = Path("/var/lib/akernel-image-snapshots")
    use_snapshots = writable_tmpfs_mb > 0 or writable_backing == "file"
    if writable_backing == "file":
        result = subprocess.run(
            [
                "findmnt",
                "--noheadings",
                "--output",
                "FSTYPE",
                "--mountpoint",
                str(snapshots),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        if result.stdout.strip() != "tmpfs":
            raise RuntimeError("sandboxd must provide the file-backed tmpfs mount")
    if writable_tmpfs_mb and writable_backing == "memory":
        # This POC uses a separate tmpfs to avoid using the root overlay as upper.
        # Writable host binds also work on the tested runsc, but need a separate
        # storage snapshot/rollback integration before replacing this path.
        # This is within the Sandbox memory budget and survives whole-Sandbox C/R.
        # Docker bind-mounts its data root at startup; keep the tmpfs outside it.
        snapshots = Path("/var/lib/akernel-image-snapshots")
        snapshots.mkdir(parents=True, exist_ok=True)
        if any(snapshots.iterdir()):
            raise RuntimeError(
                "refusing to hide existing snapshotter state under tmpfs"
            )
        subprocess.run(
            [
                "mount",
                "-t",
                "tmpfs",
                "-o",
                f"size={writable_tmpfs_mb}m",
                "worker-image-writes",
                str(snapshots),
            ],
            check=True,
        )
    directory = Path("/run/akernel-image-bridge")
    directory.mkdir(mode=0o700, exist_ok=True)
    config = root / "containerd-image-bridge.toml"
    config.write_text("""version = 2
root = "/var/lib/docker/containerd-bridge"
state = "/run/akernel-image-bridge/state"
disabled_plugins = ["io.containerd.grpc.v1.cri"]
[grpc]
  address = "/run/akernel-image-bridge/containerd.sock"
[proxy_plugins.akernel]
  type = "snapshot"
  address = "/run/akernel-image-bridge/snapshotter.sock"
""")
    for name, command in [
        (
            "snapshotter",
            [
                "/opt/akernel-image-bridge",
                "--plan",
                str(plan),
                *(["--root", str(snapshots)] if use_snapshots else []),
            ],
        ),
        ("containerd", ["containerd", "--config", str(config)]),
    ]:
        with (root / (name + ".log")).open("ab") as log:
            process = subprocess.Popen(
                command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
            )
        children.append(process)
    deadline = time.monotonic() + 60
    while True:
        if any(p.poll() is not None for p in children):
            raise RuntimeError(
                "image bridge exited; inspect snapshotter/containerd logs"
            )
        try:
            result = subprocess.run(
                ["ctr", "--address", str(directory / "containerd.sock"), "version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode == 0 and (directory / "snapshotter.sock").exists():
                break
        except subprocess.TimeoutExpired:
            pass
        if time.monotonic() > deadline:
            raise TimeoutError("image bridge readiness timeout")
        time.sleep(0.2)
    with (root / "image-registration.log").open("ab") as log:
        subprocess.run(
            ["/opt/akernel-image-bridge", "--mode", "register", "--plan", str(plan)],
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=120,
        )
        for alias, reference in json.loads(plan.read_text()).get("aliases", {}).items():
            subprocess.run(
                [
                    "ctr",
                    "--address",
                    str(directory / "containerd.sock"),
                    "--namespace",
                    "moby",
                    "images",
                    "tag",
                    "--force",
                    reference,
                    alias,
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
                timeout=30,
            )
    config = root / "docker-image-bridge.json"
    config.write_text(
        json.dumps(
            {
                "features": {"containerd-snapshotter": True},
                "containerd": str(directory / "containerd.sock"),
                "containerd-namespace": "moby",
                "storage-driver": "akernel",
            }
        )
    )
    return ["--config-file", str(config)]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root", type=Path, default=Path("/opt/akernel-docker-runtime")
    )
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--writable-tmpfs-mb", type=int, default=0)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument(
        "--writable-backing", choices=["memory", "file"], default="memory"
    )
    args = parser.parse_args()
    if args.verify:
        verify_image_bridge(args.plan)
    else:
        print(
            json.dumps(
                start_image_bridge(
                    args.root,
                    args.plan,
                    [],
                    args.writable_tmpfs_mb,
                    args.writable_backing,
                )
            )
        )
