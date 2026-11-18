"""Isolated public-API / manifest deletion / physical Registry GC qualification."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import secrets
import socket
import subprocess
import time
import uuid
from pathlib import Path

import httpx
from akernel_sdk import BackendOperationError, Client, ImageRetention

REPO = Path(__file__).resolve().parents[3]
module = importlib.util.spec_from_file_location(
    "managed_image_reclaim", REPO / "builder/managed-docker/reclaim.py"
)
reclaimer = importlib.util.module_from_spec(module)
module.loader.exec_module(reclaimer)


def run(*args):
    return subprocess.run(
        args, check=True, text=True, capture_output=True
    ).stdout.strip()


def port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def write(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def sha(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def bytes_used(root):
    return sum(p.stat().st_blocks * 512 for p in root.rglob("*") if p.is_file())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("coordinator", "apiserver", "redis", "tls-dir", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--registry-image", required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    tls = args.tls_dir.resolve()
    name = "akernel-images167-" + uuid.uuid4().hex[:12]
    services = []
    logs = []
    registry_container = None

    def start(binary, config=None, extra=()):
        log = (root / (Path(binary).name + ".log")).open("ab")
        logs.append(log)
        # Redis uses argv[0] to select server/checker mode; preserve symlinks.
        command = [str(binary.absolute()), *extra]
        if config is not None:
            command += ["--config", str(config)]
        process = subprocess.Popen(command, stdout=log, stderr=log)
        services.append(process)
        return process

    try:
        data = root / "registry"
        data.mkdir()
        cfg = root / "registry.yaml"
        cfg.write_text(
            "version: 0.1\nstorage:\n  filesystem:\n    rootdirectory: /var/lib/registry\n  delete:\n    enabled: true\nhttp:\n  addr: :5000\n"
        )
        image_id = run(
            "docker", "image", "inspect", "--format", "{{.Id}}", args.registry_image
        )
        registry_container = run(
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--label",
            "akernel.task=managed-images-167",
            "--memory",
            "512m",
            "--cpus",
            "1",
            "-p",
            f"127.0.0.1:{port()}:5000",
            "-v",
            f"{data}:/var/lib/registry",
            "-v",
            f"{cfg}:/etc/distribution/config.yml:ro",
            image_id,
        )
        mapping = run("docker", "port", registry_container, "5000/tcp").splitlines()[0]
        registry_url = "http://" + mapping
        registry_authority = mapping
        with httpx.Client() as transport:
            for _ in range(100):
                try:
                    if (
                        transport.get(registry_url + "/v2/", timeout=1).status_code
                        == 200
                    ):
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
            else:
                raise RuntimeError("owned Registry failed readiness")

        redis_dir = root / "redis"
        redis_dir.mkdir()
        redis_socket = root / "redis.sock"
        # Keep the Unix pathname within sockaddr_un even for long checkout paths.
        redis_socket = Path("/tmp") / (name + ".sock")
        start(
            args.redis,
            extra=(
                "--port",
                "0",
                "--save",
                "",
                "--appendonly",
                "yes",
                "--appendfsync",
                "always",
                "--unixsocket",
                str(redis_socket),
                "--dir",
                str(redis_dir),
            ),
        )
        coordinator_port, api_port = port(), port()
        token = secrets.token_hex(32)
        token_file = root / "test-api-key"
        token_file.write_text(token)
        token_file.chmod(0o600)
        user_token = secrets.token_hex(32)
        user_token_file = root / "test-user-key"
        user_token_file.write_text(user_token)
        user_token_file.chmod(0o600)
        coordinator_config = root / "coordinator.json"
        write(
            coordinator_config,
            {
                "listen": f"127.0.0.1:{coordinator_port}",
                "redis_url": "redis+unix://" + str(redis_socket),
                "namespace": "qualification",
                "scheduler_shards": 1,
                "placement": "pack",
                "rpc_timeout_seconds": 5,
                "managed_image_reclamation": True,
                "managed_images": {
                    "registry": {
                        "authority": registry_authority,
                        "repository_prefix": "qualification",
                    },
                },
                "tls": {
                    "mode": "mtls",
                    "ca": str(tls / "ca.pem"),
                    "certificate": str(tls / "coordinator.pem"),
                    "private_key": str(tls / "coordinator.key"),
                    "server_name": "localhost",
                    "peers": {
                        "apiserver": str(tls / "apiserver.der"),
                        "coordinator": str(tls / "coordinator.der"),
                        "node:node": str(tls / "node.der"),
                    },
                },
                "bootstrap_credentials": [
                    {
                        "key_file": str(token_file),
                        "tenant_id": "qualification",
                        "administrator": True,
                        "expires_at_unix_seconds": 0,
                    },
                    {
                        "key_file": str(user_token_file),
                        "tenant_id": "qualification",
                        "administrator": False,
                        "expires_at_unix_seconds": 0,
                    },
                ],
            },
        )
        for _ in range(100):
            if redis_socket.exists():
                break
            time.sleep(0.02)
        start(args.coordinator, coordinator_config)
        api_config = root / "apiserver.json"
        write(
            api_config,
            {
                "listen": f"127.0.0.1:{api_port}",
                "loopback_http": True,
                "ingress_mode": "standalone",
                "coordinator_address": f"https://127.0.0.1:{coordinator_port}",
                "discovery": None,
                "internal_security": "mtls",
                "ca": str(tls / "ca.pem"),
                "certificate": str(tls / "apiserver.pem"),
                "private_key": str(tls / "apiserver.key"),
                "server_name": "localhost",
                "rpc_timeout_seconds": 5,
                "cache_entries": 100,
                "auth_cache_ttl_seconds": 1,
            },
        )
        start(args.apiserver, api_config)
        client = Client(server_address=f"http://127.0.0.1:{api_port}", token=token)
        for _ in range(100):
            if any(process.poll() is not None for process in services):
                raise RuntimeError(
                    "owned control service exited; inspect retained logs"
                )
            try:
                list(client.images.list())
                break
            except BackendOperationError:
                time.sleep(0.05)
        else:
            raise RuntimeError("owned API failed readiness")

        shared = os.urandom(1024 * 1024)

        def publish(request, unique, managed=True, partial=False):
            image = (
                client.images.begin(request, ImageRetention.ephemeral(0))
                if managed
                else None
            )
            publication = (
                f"qualification/{secrets.token_hex(16)}/publications/{image.id}"
                if image
                else None
            )
            repo = f"{publication}/service-0" if publication else "legacy/keep"
            tagged = f"{registry_authority}/{repo}:build"
            if image:
                plan = {"single": tagged}
                if partial:
                    plan["absent"] = (
                        f"{registry_authority}/{publication}/service-1:build"
                    )
                image._mutate("plan", outputs=plan)
            config = b'{"architecture":"amd64","os":"linux","rootfs":{"type":"layers","diff_ids":[]}}'
            layers = [shared, unique]
            with httpx.Client() as http:
                for blob in [config, *layers]:
                    response = http.post(f"{registry_url}/v2/{repo}/blobs/uploads/")
                    response.raise_for_status()
                    location = response.headers["Location"]
                    if location.startswith("/"):
                        location = registry_url + location
                    response = http.put(
                        httpx.URL(location).copy_merge_params({"digest": sha(blob)}),
                        content=blob,
                    )
                    response.raise_for_status()
                manifest = json.dumps(
                    {
                        "schemaVersion": 2,
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "config": {
                            "mediaType": "application/vnd.oci.image.config.v1+json",
                            "digest": sha(config),
                            "size": len(config),
                        },
                        "layers": [
                            {
                                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                                "digest": sha(blob),
                                "size": len(blob),
                            }
                            for blob in layers
                        ],
                    },
                    separators=(",", ":"),
                ).encode()
                child_digest = sha(manifest)
                response = http.put(
                    f"{registry_url}/v2/{repo}/manifests/{child_digest}",
                    content=manifest,
                    headers={
                        "Content-Type": "application/vnd.oci.image.manifest.v1+json"
                    },
                )
                response.raise_for_status()
                manifest = json.dumps(
                    {
                        "schemaVersion": 2,
                        "mediaType": "application/vnd.oci.image.index.v1+json",
                        "manifests": [
                            {
                                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                                "digest": child_digest,
                                "size": len(manifest),
                                "platform": {"os": "linux", "architecture": "amd64"},
                            }
                        ],
                    },
                    separators=(",", ":"),
                ).encode()
                response = http.put(
                    f"{registry_url}/v2/{repo}/manifests/build",
                    content=manifest,
                    headers={"Content-Type": "application/vnd.oci.image.index.v1+json"},
                )
                response.raise_for_status()
            if image and not partial:
                image._mutate("publish", digests={"single": sha(manifest)})
            return image, repo, f"{registry_authority}/{repo}@{sha(manifest)}"

        doomed, _, _ = publish("owned-delete", os.urandom(2 * 1024 * 1024))
        survivor, survivor_repo, _ = publish("owned-survivor", os.urandom(1024 * 1024))
        abandoned, _, abandoned_ref = publish(
            "owned-interrupted", os.urandom(1024 * 1024), partial=True
        )
        _, legacy_repo, legacy_reference = publish(
            "unmanaged", os.urandom(1024 * 1024), managed=False
        )
        survivor.pin()
        survivor.release()
        ordinary = Client(
            server_address=f"http://127.0.0.1:{api_port}", token=user_token
        )
        from akernel_sdk import PermissionDeniedException

        try:
            ordinary.images.get(doomed.id)._mutate(
                "claim_deletion", manifests=[doomed.reference]
            )
        except PermissionDeniedException:
            pass
        else:
            raise AssertionError("ordinary SDK caller acquired delete authority")
        doomed.retain("independent-reader")
        doomed.release()
        registry = reclaimer.Registry(
            registry_url, allow_http=True, repository_prefix="qualification"
        )
        try:
            if not reclaimer.reclaim(survivor, registry).get("protected"):
                raise AssertionError(
                    "pinned image lost protection after publisher release"
                )
            protected = reclaimer.reclaim(doomed, registry)
            if not protected.get("protected"):
                raise AssertionError("independent reader lost protection")
            doomed.release("independent-reader")
            dry = reclaimer.reclaim(doomed, registry)
            if not dry.get("dry_run") or registry.read(doomed.reference) is None:
                raise AssertionError("dry-run altered the manifest")
            before = bytes_used(data)
            # Simulate an operator crash after claiming and deleting only one
            # manifest. The next invocation must resume the durable worklist.
            doomed._mutate("claim_deletion", manifests=dry["manifests"])
            registry.delete(dry["manifests"][0])
            deleted = reclaimer.reclaim(doomed, registry, apply=True)
            if deleted.get("state") != "deleted":
                raise AssertionError("manifest deletion did not finish")
            if deleted.get("manifest_already_absent") != 1:
                raise AssertionError("partial deletion was not reconciled idempotently")
            if registry.read(survivor.reference) is None:
                raise AssertionError("shared survivor manifest disappeared")
            if not reclaimer.reclaim(abandoned, registry).get("protected"):
                raise AssertionError("unfinished publication lost protection")
            # The synchronous fixture writer has returned and cannot write again.
            abandoned_receipt = reclaimer.reconcile_abandoned(
                abandoned, registry, "qualification:fixture-writer-exited"
            )
            if abandoned_receipt["state"] != "abandoned" or abandoned_receipt[
                "outputs"
            ] != {"single": abandoned_ref}:
                raise AssertionError(
                    "partial reconciliation committed the wrong outputs"
                )
            if reclaimer.reclaim(abandoned, registry, apply=True)["state"] != "deleted":
                raise AssertionError("verified partial publication was not reclaimed")
        finally:
            registry.close()
        after_manifest = bytes_used(data)
        run("docker", "stop", registry_container)
        gc_args = [
            "docker",
            "run",
            "--rm",
            "--name",
            name + "-gc",
            "--network",
            "none",
            "--label",
            "akernel.task=managed-images-167",
            "--memory",
            "512m",
            "--cpus",
            "1",
            "-v",
            f"{data}:/var/lib/registry",
            "-v",
            f"{cfg}:/etc/distribution/config.yml:ro",
            image_id,
            "garbage-collect",
        ]
        (root / "gc-dry-run.log").write_text(
            run(*gc_args, "--dry-run", "/etc/distribution/config.yml")
        )
        if bytes_used(data) != after_manifest:
            raise AssertionError("GC dry-run changed storage bytes")
        (root / "gc.log").write_text(run(*gc_args, "/etc/distribution/config.yml"))
        after_gc = bytes_used(data)
        if after_gc >= after_manifest:
            raise AssertionError("GC did not physically reclaim data")
        run("docker", "start", registry_container)
        with httpx.Client() as http:
            retained = None
            for _ in range(100):
                try:
                    retained = http.get(
                        f"{registry_url}/v2/{survivor_repo}/blobs/{sha(shared)}"
                    )
                    if retained.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
            if retained is None or retained.status_code != 200:
                raise RuntimeError("restarted owned Registry did not become ready")
            if retained.content != shared:
                raise AssertionError("shared retained blob was damaged")
            legacy = http.get(
                f"{registry_url}/v2/{legacy_repo}/manifests/{legacy_reference.rsplit('@', 1)[1]}"
            )
            legacy.raise_for_status()
            if sha(legacy.content) != legacy_reference.rsplit("@", 1)[1]:
                raise AssertionError("unmanaged legacy fixture was damaged")
        receipt = {
            "registry_image_id": image_id,
            "container": name,
            "before_manifest_delete_bytes": before,
            "after_manifest_delete_bytes": after_manifest,
            "after_gc_bytes": after_gc,
            "physically_reclaimed_bytes": after_manifest - after_gc,
            "shared_blob_preserved": True,
            "unmanaged_manifest_preserved": True,
            "ordinary_delete_denied": True,
            "partial_publication_reconciled": True,
            "manifest_delete": deleted,
            "native_runtime_checkpoint_test": False,
        }
        (root / "summary.json").write_text(json.dumps(receipt, indent=2))
        print(json.dumps(receipt, sort_keys=True))
    finally:
        for process in reversed(services):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for log in logs:
            log.close()
        if registry_container is not None:
            output = subprocess.run(
                ["docker", "logs", registry_container],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            (root / "registry.log").write_text(output.stdout)
            run("docker", "rm", "-f", registry_container)


if __name__ == "__main__":
    main()
