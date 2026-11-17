"""Runtime-side Compose/Buildx adapter. Docker owns Dockerfile/Compose semantics.

The client uploads a context and invokes this tool; image metadata and ROFS
registration remain on the runtime side. This preview uses an operator-issued
session capability and pins its complete collection until session cleanup.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener


def execute(args: list[str], *, capture: bool = False) -> str:
    result = subprocess.run(
        args, check=True, text=True, stdout=subprocess.PIPE if capture else None
    )
    return result.stdout if capture else ""


def execute_bake(args: list[str], plan: dict) -> None:
    """Retry a completed Nydus variant export failure using the same native plan.

    Buildx progress is streamed while retaining only a bounded diagnostic tail.
    Native no-cache targets have a single attempt. Transport/process exceptions
    propagate with their original identity rather than becoming another solve.
    """
    if args[:3] != ["docker", "buildx", "bake"]:
        raise ValueError("Expected the native Buildx Bake command")
    attempts = 1 if any(t.get("no-cache") for t in plan["target"].values()) else 3
    for attempt in range(attempts):
        tail = b""
        with subprocess.Popen(args, stderr=subprocess.PIPE) as process:
            assert process.stderr is not None
            while chunk := process.stderr.read(4096):
                sys.stderr.buffer.write(chunk)
                sys.stderr.buffer.flush()
                tail = (tail + chunk)[-65536:]
            code = process.wait()
        if code == 0:
            return
        error = subprocess.CalledProcessError(code, args, stderr=tail)
        terminal = [
            line
            for line in tail.decode(errors="replace").splitlines()
            if line.startswith("ERROR:")
        ]
        known_export_failure = (
            code == 1
            and terminal
            and any(
                message in terminal[-1]
                for message in (
                    'failed to get compression blob "nydus": not found',
                    'merge nydus layers: get compression blob "nydus": not found',
                )
            )
        )
        if not known_export_failure or attempt + 1 == attempts:
            raise error
        print(
            f"AKernel: native Nydus export exited {code}; retry {attempt + 2}/{attempts} "
            "with the same Bake plan and native cache (first failure retained above).",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(1)


def bake_metadata(args: list[str], plan: dict, path: Path, profile: dict) -> dict:
    """Reconcile narrowly identified result errors against published outputs.

    A native build is never replayed here. Each invocation publishes unpredictable
    tags; Registry inspection must verify every active target before adopting its
    immutable digest. Ordinary solve, authorization and process errors propagate.
    """
    os.environ.setdefault("BUILDX_METADATA_PROVENANCE", "disabled")
    path.unlink(missing_ok=True)
    receipt_path = path.with_name(path.name + ".publication.json")
    receipt_path.unlink(missing_ok=True)
    try:
        execute_bake(args, plan)
    except subprocess.CalledProcessError as error:
        tail = error.stderr or b""
        if isinstance(tail, bytes):
            tail = tail.decode(errors="replace")
        terminal = [line for line in tail.splitlines() if line.startswith("ERROR:")]
        history_failure = bool(terminal) and re.fullmatch(
            r"ERROR: .*unknown blob sha256:[0-9a-f]{64} in history", terminal[-1]
        )
        publication_deadline = bool(terminal) and terminal[-1] == (
            "ERROR: failed to solve: Internal: context deadline exceeded"
        )
        if error.returncode != 100 or not (history_failure or publication_deadline):
            raise
        try:
            outputs = {}
            tags = set()
            prefix = profile["registry"] + "/" + profile["prefix"].rstrip("/") + "/"
            names = plan["group"]["default"]["targets"]
            if not names:
                raise ValueError("No native outputs to reconcile")
            for name in names:
                tagged = plan["target"][name]["tags"]
                if len(tagged) != 1 or not tagged[0].startswith(prefix):
                    raise ValueError("Output is not in this operation's repository")
                tag = tagged[0].rsplit(":", 1)[-1]
                if not re.fullmatch(r"build-[0-9a-f]{32}", tag):
                    raise ValueError("Output lacks a fresh publication tag")
                tags.add(tag)
                outputs[name] = tagged[0]
            if len(tags) != 1:
                raise ValueError("Outputs belong to different build invocations")
            acknowledged = {}
            if publication_deadline:
                for name, tagged in outputs.items():
                    if plan["target"][name].get("attest"):
                        raise ValueError(
                            "Explicit attestations require native completion"
                        )
                    digests = set(
                        re.findall(
                            r"(?m)^#\d+ pushing manifest for "
                            + re.escape(tagged)
                            + r"@(sha256:[0-9a-f]{64})[^\r\n]* done\r?$",
                            tail,
                        )
                    )
                    if len(digests) != 1:
                        raise ValueError(
                            "No complete native manifest push acknowledgement"
                        )
                    acknowledged[name] = digests.pop()
            recovered = {}
            for name, tagged in outputs.items():
                inspection = json.loads(
                    execute(
                        [
                            "docker",
                            "manifest",
                            "inspect",
                            "--verbose",
                            *(
                                ["--insecure"]
                                if profile.get("registry_insecure", False)
                                else []
                            ),
                            tagged,
                        ],
                        capture=True,
                    )
                )
                if not isinstance(inspection, dict):
                    raise TypeError("Expected one native linux/amd64 image manifest")
                descriptor = inspection["Descriptor"]
                raw = base64.b64decode(inspection["Raw"], validate=True)
                manifest = json.loads(raw)
                digest = "sha256:" + hashlib.sha256(raw).hexdigest()
                if name in acknowledged and acknowledged[name] != digest:
                    raise ValueError(
                        "Registry digest differs from the native push acknowledgement"
                    )
                if (
                    descriptor.get("digest") != digest
                    or descriptor.get("size") != len(raw)
                    or descriptor.get("mediaType")
                    != "application/vnd.oci.image.manifest.v1+json"
                    or descriptor.get("platform", {}).get("os") != "linux"
                    or descriptor.get("platform", {}).get("architecture") != "amd64"
                ):
                    raise ValueError(
                        "Registry manifest digest, size or platform mismatch"
                    )
                if (
                    not isinstance(manifest, dict)
                    or manifest.get("schemaVersion") != 2
                    or manifest.get("mediaType") != descriptor["mediaType"]
                    or not isinstance(manifest.get("config"), dict)
                    or not isinstance(manifest.get("layers"), list)
                ):
                    raise ValueError(
                        "Registry did not return a native OCI image manifest"
                    )
                recovered[name] = {
                    "containerimage.digest": digest,
                    "akernel.publication-reconciled": True,
                }
            receipt_path.write_text(
                json.dumps(
                    {
                        "native_exit_code": error.returncode,
                        "native_error": terminal[-1],
                        **({"history_error": terminal[-1]} if history_failure else {}),
                        "failure_kind": "history-result"
                        if history_failure
                        else "acknowledged-publication-deadline",
                        "outputs": {
                            name: {"tagged": outputs[name], **data}
                            for name, data in recovered.items()
                        },
                        "verification": "Fresh invocation tags; official Docker Registry inspection; raw manifest SHA256, size and platform verified",
                        "native_build_replayed": False,
                    },
                    indent=2,
                )
            )
            path.write_text(json.dumps(recovered, indent=2))
        except Exception as verification:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            error.add_note(
                "Published output verification failed: " + type(verification).__name__
            )
            raise error from verification
        print(
            "AKernel: native image publication verified after a native result error; "
            "using committed immutable artifacts without replaying the build.",
            file=sys.stderr,
            flush=True,
        )
        return recovered
    return json.loads(path.read_text())


def compose_command(descriptor: dict) -> list[str]:
    command = [
        "docker",
        "compose",
        "-p",
        descriptor["project_name"],
        "--project-directory",
        descriptor["directory"],
    ]
    for filename in descriptor["files"]:
        command.extend(["-f", filename])
    return command


def describe(descriptor: dict) -> list[str]:
    """Resolve service identities natively, without exposing interpolated secrets."""
    os.environ.update(descriptor.get("environment", {}))
    config = json.loads(
        execute(
            [*compose_command(descriptor), "config", "--format", "json"], capture=True
        )
    )
    services = config.get("services", {})
    if not services:
        raise ValueError("Compose project requires at least one active service")
    return list(services)


def build_plan(
    descriptor: dict,
    config: dict,
    bake: dict,
    registry: str,
    prefix: str,
    no_cache: bool = False,
    registry_insecure: bool = False,
    publication_tag: str = "built",
) -> tuple[dict, dict[str, str], dict[str, str]]:
    """Adapt native Bake targets' output only; do not interpret Dockerfiles."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9/_-]*", prefix) or ".." in prefix:
        raise ValueError("Invalid session repository prefix")
    if not re.fullmatch(r"built|build-[0-9a-f]{32}", publication_tag):
        raise ValueError("Invalid native publication tag")
    targets = bake.setdefault("target", {})
    native_services = {}
    for target_name, target in targets.items():
        service_name = target.get("labels", {}).get("com.docker.compose.service")
        if service_name is not None:
            if service_name in native_services:
                raise ValueError(
                    "Ambiguous native Bake targets for service: " + service_name
                )
            native_services[service_name] = target_name
    references = {}
    service_targets = {}
    for index, (name, service) in enumerate(config["services"].items()):
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name):
            raise ValueError("Unsupported Compose service name")
        if service.get("build") is not None:
            target_name = native_services.get(name)
            # Exact names also support explicitly constructed single-image
            # plans. Never guess Compose's name normalization or disregard
            # a conflicting native service identity.
            if (
                target_name is None
                and name in targets
                and "com.docker.compose.service" not in targets[name].get("labels", {})
            ):
                target_name = name
            if target_name is None:
                raise ValueError("No native Bake target for build service: " + name)
            target = targets[target_name]
        else:
            # Image-only inputs use the official Dockerfile frontend for format
            # conversion, retaining OCI config. No Python layer reconstruction.
            image = service.get("image")
            if (
                not isinstance(image, str)
                or not image
                or any(c.isspace() for c in image)
            ):
                raise ValueError("Service requires an image or native build target")
            target = {"dockerfile-inline": "FROM " + image + "\n"}
            target_name = "akernel_image_" + str(index)
            while target_name in targets:
                target_name += "_"
            targets[target_name] = target
        service_targets[name] = target_name
        # Publication names are private invocation-local identities, not
        # lowercased service names. An ordinal is injective even for Foo/foo or
        # dotted/underscored names, and bounds repository-component length.
        reference = (
            registry
            + "/"
            + prefix.rstrip("/")
            + "/service-"
            + str(index)
            + ":"
            + publication_tag
        )
        references[name] = reference
        target.update(
            {
                "tags": [reference],
                "platforms": ["linux/amd64"],
                "attest": [],
                "output": [
                    "type=image,push=true,compression=nydus,force-compression=true,"
                    "oci-mediatypes=true,fs-version=5"
                    + (",registry.insecure=true" if registry_insecure else "")
                ],
            }
        )
        if no_cache:
            target["no-cache"] = True
    bake["group"] = {"default": {"targets": list(service_targets.values())}}
    return bake, references, service_targets


def build(descriptor: dict, profile: dict, *, no_cache: bool = False) -> dict:
    os.environ.setdefault("BUILDX_NO_DEFAULT_ATTESTATIONS", "1")
    os.environ.update(descriptor.get("environment", {}))
    command = compose_command(descriptor)
    config = json.loads(execute([*command, "config", "--format", "json"], capture=True))
    printed = execute([*command, "build", "--print"], capture=True)
    if printed.strip():
        bake = json.loads(printed)
    elif any(
        service.get("build") is not None for service in config["services"].values()
    ):
        raise ValueError("Compose returned an empty Bake plan for build services")
    else:
        bake = {}
    bake, targets, service_targets = build_plan(
        descriptor,
        config,
        bake,
        profile["registry"],
        profile["prefix"],
        no_cache,
        profile.get("registry_insecure", False),
        "build-" + uuid.uuid4().hex,
    )
    root = Path(descriptor["directory"] + ".build")
    root.mkdir(mode=0o700, exist_ok=True)
    plan_path = root / "bake.json"
    plan_path.write_text(json.dumps(bake))
    (root / "outputs.json").write_text(
        json.dumps(
            {
                name: {"target": service_targets[name], "reference": tagged}
                for name, tagged in targets.items()
            },
            indent=2,
        )
    )
    metadata_path = root / "metadata.json"
    metadata = bake_metadata(
        [
            "docker",
            "buildx",
            "bake",
            "--builder",
            profile["builder"],
            "--file",
            str(plan_path),
            "--metadata-file",
            str(metadata_path),
            "--progress",
            "plain",
            "default",
        ],
        bake,
        metadata_path,
        profile,
    )
    references = {}
    for name, tagged in targets.items():
        digest = metadata[service_targets[name]]["containerimage.digest"]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError("BuildKit did not return an immutable image digest")
        references[name] = tagged.rsplit(":", 1)[0] + "@" + digest
    (root / "artifacts.json").write_text(json.dumps(references, indent=2))
    return references


def prepare(descriptor: dict, profile: dict) -> dict:
    root = Path(descriptor["directory"] + ".build")
    references = json.loads((root / "artifacts.json").read_text())
    token = Path(profile["token_file"]).read_text().strip()
    opener = build_opener(ProxyHandler({}))
    prepared = []
    for reference in dict.fromkeys(references.values()):
        request = Request(
            profile["provider"] + "/prepare",
            data=json.dumps({"reference": reference}).encode(),
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
        )
        with opener.open(request, timeout=180) as response:
            prepared.append(json.loads(response.read(16 * 1024)))
    execute(
        [
            "/opt/akernel-image-bridge",
            "--mode",
            "register",
            "--plan",
            profile["plan"],
            "--containerd",
            "/run/akernel-image-bridge/containerd.sock",
        ]
    )
    # Generated override; never mutate the supplied Compose or Dockerfiles.
    lines = ["services:"]
    for service, reference in references.items():
        lines.extend(
            [
                "  " + json.dumps(service) + ":",
                "    build: !reset null",
                "    image: " + json.dumps(reference),
                "    pull_policy: never",
            ]
        )
    overlay = str(root / "runtime-images.yaml")
    Path(overlay).write_text("\n".join(lines) + "\n")
    attached = dict(descriptor)
    attached["files"] = [*descriptor["files"], overlay]
    attached["managed_artifacts"] = references
    (root / "attached.json").write_text(json.dumps(attached, indent=2))
    return {"artifacts": references, "prepared": prepared, "descriptor": attached}


def image_request(profile: dict, reference: str, operation: str) -> dict:
    token = Path(profile["token_file"]).read_text().strip()
    request = Request(
        profile["provider"] + operation,
        data=json.dumps({"reference": reference}).encode(),
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
    )
    with build_opener(ProxyHandler({})).open(request, timeout=180) as response:
        return json.loads(response.read(1024 * 1024))


def prepare_image(profile: dict, reference: str) -> dict:
    return image_request(profile, reference, "/prepare")


def describe_image(profile: dict, reference: str) -> dict:
    return image_request(profile, reference, "/describe")


def start_builder(profile: dict) -> None:
    execute(
        [
            "docker",
            "buildx",
            "create",
            "--name",
            profile["builder"],
            "--driver",
            "remote",
            profile["endpoint"],
            "--use",
        ]
    )


def image_build(context: str, profile: dict, *, no_cache: bool = False) -> dict:
    os.environ.setdefault("BUILDX_NO_DEFAULT_ATTESTATIONS", "1")
    start_builder(profile)
    metadata = context + ".metadata.json"
    plan, targets, service_targets = build_plan(
        {},
        {"services": {"single": {"build": {}}}},
        {"target": {"single": {"context": context}}},
        profile["registry"],
        profile["prefix"],
        no_cache,
        profile.get("registry_insecure", False),
        "build-" + uuid.uuid4().hex,
    )
    plan_path = context + ".bake.json"
    Path(plan_path).write_text(json.dumps(plan))
    values = bake_metadata(
        [
            "docker",
            "buildx",
            "bake",
            "--builder",
            profile["builder"],
            "--file",
            plan_path,
            "--progress",
            "plain",
            "--metadata-file",
            metadata,
            "default",
        ],
        plan,
        Path(metadata),
        profile,
    )
    exported = service_targets["single"]
    digest = values[exported]["containerimage.digest"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("BuildKit did not return an immutable artifact")
    return describe_image(profile, targets["single"].rsplit(":", 1)[0] + "@" + digest)


def expose_service(container: str, port: int) -> dict:
    import sys

    sys.path.insert(0, "/usr/lib/akernel")
    from docker_network_guest import expose

    data = json.loads(execute(["docker", "inspect", container], capture=True))[0]
    seen = set()
    for _ in range(8):
        mode = data["HostConfig"]["NetworkMode"]
        if not mode.startswith("container:"):
            break
        owner = mode.removeprefix("container:")
        if not owner or owner in seen:
            raise ValueError("Invalid network namespace ownership")
        seen.add(owner)
        data = json.loads(execute(["docker", "inspect", owner], capture=True))[0]
    else:
        raise ValueError("Network namespace ownership exceeds limit")
    if data["HostConfig"]["NetworkMode"] == "host":
        return {"url": f"http://127.0.0.1:{port}"}
    connected = []
    for name, network in (data["NetworkSettings"].get("Networks") or {}).items():
        if not network.get("IPAddress"):
            continue
        network = dict(network)
        if not network.get("Gateway"):
            inspected = json.loads(
                execute(["docker", "network", "inspect", name], capture=True)
            )[0]
            import ipaddress

            address = ipaddress.ip_address(network["IPAddress"])
            gateways = [
                v.get("Gateway")
                for v in inspected.get("IPAM", {}).get("Config", [])
                if v.get("Subnet")
                and address in ipaddress.ip_network(v["Subnet"], strict=False)
            ]
            network["Gateway"] = next((v for v in gateways if v), None)
        if network.get("Gateway"):
            connected.append(network)
    if not connected:
        raise ValueError("Managed service requires an IPv4 bridge")
    import uuid

    value = connected[0]
    # The runtime helper owns the relay process; return its opaque endpoint.
    return expose(
        Path("/opt/akernel-docker-runtime/tunnels") / uuid.uuid4().hex,
        {
            "address": value["Gateway"],
            "allowed": [value["IPAddress"]],
            "target_port": port,
        },
    )


def start_runtime(profile: dict) -> None:
    import sys

    sys.path.insert(0, "/usr/lib/akernel")
    from docker_network_guest import configure
    from image_bridge_guest import start_image_bridge

    if Path("/var/run/docker.sock").exists():
        raise RuntimeError("Runtime startup is fresh-only")
    runtime = Path("/opt/akernel-docker-runtime")
    runtime.mkdir(exist_ok=True)
    flags = start_image_bridge(
        runtime,
        Path(profile["plan"]),
        [],
        writable_tmpfs_mb=profile["writable_mb"],
        writable_backing="file",
    )
    configure()
    env = dict(os.environ)
    env["PATH"] = str(runtime / "network-bin") + ":" + env["PATH"]
    with Path("/var/log/akernel-dockerd.log").open("a") as logs:
        process = subprocess.Popen(
            [
                "dockerd",
                *flags,
                "--iptables=false",
                "--ip6tables=false",
                "--userland-proxy=true",
            ],
            stdout=logs,
            stderr=logs,
            env=env,
            start_new_session=True,
        )
    for _ in range(40):
        if process.poll() is not None:
            raise RuntimeError("Docker exited; inspect /var/log/akernel-dockerd.log")
        if (
            subprocess.run(
                ["docker", "info"], capture_output=True, check=False
            ).returncode
            == 0
        ):
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("Docker readiness timed out")
    start_builder(profile)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "operation",
        choices=[
            "start",
            "build",
            "prepare",
            "describe",
            "image-build",
            "reconnect",
            "expose",
        ],
    )
    parser.add_argument("--profile", default="/run/akernel-build/profile.json")
    parser.add_argument("--descriptor")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--context")
    parser.add_argument("--result")
    parser.add_argument("--container")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    profile = json.loads(Path(args.profile).read_text())
    if args.operation == "start":
        start_runtime(profile)
    elif args.operation == "image-build":
        if not args.context or not args.result:
            parser.error("image-build requires context and result")
        Path(args.result).write_text(
            json.dumps(image_build(args.context, profile, no_cache=args.no_cache))
        )
    elif args.operation == "reconnect":
        import sys

        sys.path.insert(0, "/usr/lib/akernel")
        from docker_network_guest import configure

        configure(reconnect=True)
    elif args.operation == "expose":
        if not args.container or not args.port or not 1 <= args.port <= 65535:
            parser.error("expose requires container and valid port")
        result = expose_service(args.container, args.port)
        if result:
            print(json.dumps(result))
    else:
        descriptor = json.loads(Path(args.descriptor).read_text())
        if args.operation == "describe":
            result = describe(descriptor)
        elif args.operation == "build":
            result = build(descriptor, profile, no_cache=args.no_cache)
        else:
            result = prepare(descriptor, profile)
        print(json.dumps(result))
