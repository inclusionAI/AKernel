#!/usr/bin/env python3
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Publish a recoverable ADX state/fileset, with the completion record last."""

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import socket
import ssl
import stat
import subprocess
import tempfile
import time
import uuid
from urllib.parse import quote
from urllib.request import Request, urlopen


def relative(value):
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(p in ("", ".", "..") for p in value.split("/")) or "\\" in value:
        raise ValueError("unsafe backup-relative path")
    return path


class Mount:
    """Do not write to a local fallback after a FUSE mount is replaced."""

    def __init__(self, root):
        self.root = Path(root)
        if self.root.is_symlink() or not self.root.is_absolute():
            raise ValueError("an absolute JuiceFS mount is required")
        self.identity = self._identity()
        self.check()

    def _identity(self):
        s = self.root.stat()
        return s.st_dev, s.st_ino

    def check(self):
        if self.root.is_symlink() or self._identity() != self.identity:
            raise RuntimeError("JuiceFS mount identity changed")
        mounted = any(
            line.split()[4] == str(self.root) and " - fuse.juicefs " in line
            for line in Path("/proc/self/mountinfo").read_text().splitlines()
        )
        if not mounted:
            raise RuntimeError("JuiceFS mount is missing")


class Redis:
    """Small RESP2 client; authentication is never placed in an argv or URL."""

    def __init__(self, host, port, password, timeout=10):
        self.socket = socket.create_connection((host, port), timeout)
        self.file = self.socket.makefile("rb")
        self.command("AUTH", password)

    def close(self):
        self.file.close()
        self.socket.close()

    def command(self, *args):
        args = [str(a).encode() for a in args]
        self.socket.sendall(b"*%d\r\n" % len(args) + b"".join(
            b"$%d\r\n" % len(a) + a + b"\r\n" for a in args))
        return self._read()

    def _read(self):
        line = self.file.readline(1024 * 1024)
        if not line.endswith(b"\r\n"):
            raise RuntimeError("invalid Redis response")
        kind, value = line[:1], line[1:-2]
        if kind == b"-":
            raise RuntimeError("Redis command rejected")
        if kind == b"+":
            return value.decode()
        if kind == b":":
            return int(value)
        if kind == b"$":
            size = int(value)
            if size == -1:
                return None
            if size < 0 or size > 64 * 1024 * 1024:
                raise RuntimeError("Redis response exceeds backup limit")
            data = self.file.read(size)
            if len(data) != size or self.file.read(2) != b"\r\n":
                raise RuntimeError("truncated Redis response")
            return data.decode()
        if kind == b"*":
            size = int(value)
            if size < 0 or size > 1000000:
                raise RuntimeError("invalid Redis array size")
            return [self._read() for _ in range(size)]
        raise RuntimeError("unexpected Redis response type")


def primary_identity(redis):
    info = {}
    for section in ("server", "replication"):
        info.update(line.split(":", 1) for line in redis.command("INFO", section).splitlines()
                    if line and not line.startswith("#") and ":" in line)
    if info.get("role") != "master":
        raise RuntimeError("backup endpoint is not the Redis primary")
    # A first-ever SYNC initializes the backlog and changes master_replid even
    # without a failover. This production profile requires live replication;
    # do not enter a consistency window before that identity is established.
    if info.get("repl_backlog_active") != "1" or int(info.get("connected_slaves", "0")) < 1:
        raise RuntimeError("backup requires an initialized primary with a live replica")
    return info["run_id"], info["master_replid"]


def references(redis, namespace, alias):
    """Read the pinned ADX control:v1 schema while writes/expiry are paused."""
    key = f"adx:{{{namespace}}}:control:v1"
    raw = redis.command("HGETALL", key)
    fields = dict(zip(raw[::2], raw[1::2]))
    header = json.loads(fields.get("header", "null"))
    if not isinstance(header, dict) or header.get("schema") != 1:
        raise RuntimeError("unsupported ADX control schema")
    found = {}

    def visit(value):
        if isinstance(value, dict):
            if {"storage", "location", "size_bytes"} <= value.keys():
                if value["storage"] != alias:
                    raise RuntimeError("backup contains an unsupported storage reference")
                location = str(uuid.UUID(value["location"]))
                if location != value["location"]:
                    raise ValueError("noncanonical artifact identity")
                found[location] = value
            else:
                for item in value.values():
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for field, value in fields.items():
        if field.startswith("environment:"):
            visit(json.loads(value).get("result"))
    for value in redis.command("HVALS", key + ":snapshots"):
        snapshot = json.loads(value)
        if snapshot["state"] == "Ready" or snapshot["references"]:
            visit(snapshot["artifact"])
    for value in redis.command("HVALS", key + ":input-holds"):
        visit(json.loads(value)["artifacts"])
    return found


@contextmanager
def paused(redis, milliseconds):
    """A bounded window: never publish a set if pause expires or primary changes."""
    identity = primary_identity(redis)
    started = time.monotonic()
    redis.command("CLIENT", "PAUSE", milliseconds, "WRITE")
    deadline = started + milliseconds / 1000 - 2

    def check():
        if time.monotonic() >= deadline or primary_identity(redis) != identity:
            raise RuntimeError("consistent backup window was lost")

    try:
        yield check, deadline
        check()
    finally:
        # Redis also resumes automatically if this process/node is lost.
        redis.command("CLIENT", "UNPAUSE")


def validate_fileset(root, expected):
    """A successful backup must include every referenced committed artifact."""
    root = Path(root)
    marker = root / "cached-layout"
    if marker.is_symlink() or marker.read_bytes() != b"cached-filesystem-v1\n":
        raise RuntimeError("unsupported shared checkpoint layout")
    selected = {"cached-layout"}
    if (root / "artifacts").is_dir():
        selected.add("artifacts")

    def plain(path, directory=False):
        for parent in [path, *path.parents]:
            if parent == root:
                break
            if parent.is_symlink():
                raise RuntimeError("symlink in checkpoint fileset")
        mode = path.stat().st_mode
        if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
            raise RuntimeError("unexpected checkpoint entry type")
        return path

    for artifact_id, artifact in expected.items():
        if str(uuid.UUID(artifact_id)) != artifact_id:
            raise ValueError("invalid artifact identity")
        base = root / "artifacts" / artifact_id
        manifest_path = plain(base / "manifest.json")
        if manifest_path.stat().st_size > 4 * 1024 * 1024:
            raise RuntimeError("invalid committed artifact manifest")
        manifest = json.loads(manifest_path.read_bytes())
        if manifest["version"] != 1 or manifest["size"] != artifact["size_bytes"]:
            raise RuntimeError("artifact manifest does not match control state")
        stem = f"artifacts/{artifact_id}"
        selected.update((stem, stem + "/files", stem + "/manifest.json"))
        total = 0
        seen = set()
        for entry in manifest["files"]:
            path = relative(entry["path"])
            if entry["path"] in seen or not 0 <= entry["mode"] <= 0o777:
                raise RuntimeError("duplicate path or invalid checkpoint mode")
            seen.add(entry["path"])
            candidate = plain(base / "files" / path)
            s = candidate.stat()
            if not stat.S_ISREG(s.st_mode) or s.st_size != entry["size"]:
                raise RuntimeError("checkpoint size or type mismatch")
            with candidate.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != entry["sha256"]:
                raise RuntimeError("checkpoint digest mismatch")
            total += s.st_size
            target = PurePosixPath(stem) / "files" / path
            selected.add(str(target))
            selected.update(str(p) for p in target.parents if str(p) != ".")
        for directory in manifest["directories"]:
            plain(base / "files" / relative(directory), directory=True)
            selected.add(str(PurePosixPath(stem) / "files" / relative(directory)))
        if total != manifest["size"]:
            raise RuntimeError("checkpoint aggregate size mismatch")
    return sorted(selected)


def recovery_configuration(objects):
    """Read only the explicitly RBAC-authorized recovery inputs."""
    base = Path("/var/run/secrets/kubernetes.io/serviceaccount")
    context = ssl.create_default_context(cafile=str(base / "ca.crt"))
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
    result = []
    for namespace, kinds in objects.items():
        for kind, names in kinds.items():
            if kind not in ("secrets", "configmaps"):
                raise ValueError("unsupported recovery object kind")
            for name in names:
                path = "/api/v1/namespaces/" + quote(namespace, safe="") + "/" + kind + "/" + quote(name, safe="")
                request = Request(f"https://{host}:{port}{path}", headers={
                    "Authorization": "Bearer " + (base / "token").read_text().strip(),
                })
                with urlopen(request, context=context, timeout=10) as response:
                    result.append(json.load(response))
    return result


def oss_bucket(config):
    import oss2
    from oss2.credentials import Credentials, CredentialsProvider
    from alibabacloud_credentials.client import Client
    from alibabacloud_credentials.models import Config

    mode = config.get("credentialsMode", "oidc")
    if mode == "oidc":
        client = Client(Config(
            type="oidc_role_arn", role_arn=os.environ["ALIBABA_CLOUD_ROLE_ARN"],
            oidc_provider_arn=os.environ["ALIBABA_CLOUD_OIDC_PROVIDER_ARN"],
            oidc_token_file_path=os.environ["ALIBABA_CLOUD_OIDC_TOKEN_FILE"],
            role_session_name="akernel-checkpoint-backup", role_session_expiration=3600,
        ))
    elif mode == "sts":
        client = Client(Config(
            type="sts", access_key_id=os.environ["ALIBABA_CLOUD_ACCESS_KEY_ID"],
            access_key_secret=os.environ["ALIBABA_CLOUD_ACCESS_KEY_SECRET"],
            security_token=os.environ["ALIBABA_CLOUD_SECURITY_TOKEN"],
        ))
    else:
        raise ValueError("backup requires explicit OIDC or temporary STS credentials")

    class Provider(CredentialsProvider):
        def get_credentials(self):
            credential = client.get_credential()
            return Credentials(credential.get_access_key_id(), credential.get_access_key_secret(),
                               credential.get_security_token())

    if not config["endpoint"].startswith("https://"):
        raise ValueError("OSS HTTPS endpoint required")
    return oss2.Bucket(oss2.ProviderAuthV4(Provider()), config["endpoint"], config["bucket"],
                       region=config["region"], connect_timeout=30)


def upload(bucket, key, stream, check=lambda: None, before_complete=lambda: None):
    from oss2.models import PartInfo
    upload_id = bucket.init_multipart_upload(key).upload_id
    parts, size, digest = [], 0, hashlib.sha256()
    try:
        while True:
            check()
            block = stream.read(128 * 1024 * 1024)
            if not block:
                break
            if len(parts) == 10000:
                raise RuntimeError("backup exceeds multipart capacity")
            number = len(parts) + 1
            result = bucket.upload_part(key, upload_id, number, block)
            parts.append(PartInfo(number, result.etag))
            size += len(block)
            digest.update(block)
        if not parts:
            raise RuntimeError("empty backup payload")
        before_complete()
        check()
        bucket.complete_multipart_upload(key, upload_id, parts,
                                         headers={"x-oss-forbid-overwrite": "true"})
    except BaseException:
        bucket.abort_multipart_upload(key, upload_id)
        raise
    return {"key": key, "size": size, "sha256": digest.hexdigest()}


def upload_command(bucket, key, argv, check):
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=errors)
        try:
            def complete():
                if process.wait(timeout=30):
                    raise RuntimeError("backup export command failed")
            return upload(bucket, key, process.stdout, check, complete)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            process.stdout.close()


def run(config):
    os.umask(0o077)
    mount = Mount(config["mountRoot"])
    prefix = relative(config["checkpointPrefix"])
    staging = relative(config.get("clonePrefix", ".backup-staging"))
    if prefix == staging or prefix in staging.parents or staging in prefix.parents:
        raise ValueError("backup clone must be outside the Coordinator GC prefix")
    backup_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + str(uuid.uuid4())
    work = Path(config.get("workRoot", "/work")) / backup_id
    work.mkdir(parents=True, exist_ok=False)
    clone = mount.root / staging / backup_id
    mount.check()
    clone.parent.mkdir(parents=True, exist_ok=True)
    redis = Redis(os.environ["REDIS_HOST"], int(os.environ.get("REDIS_PORT", "6379")),
                  os.environ["REDISCLI_AUTH"])
    try:
        recovery = recovery_configuration(config.get("recoveryObjects", {}))
        with paused(redis, config.get("pauseMilliseconds", 45000)) as (check, deadline):
            expected = references(redis, config["redisNamespace"], config["storageAlias"])
            check()
            subprocess.run(["redis-cli", "-h", os.environ["REDIS_HOST"], "-p",
                            os.environ.get("REDIS_PORT", "6379"), "--rdb", str(work / "redis.rdb")],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
                           timeout=max(1, deadline - time.monotonic()))
            check()
            mount.check()
            subprocess.run(["juicefs", "clone", "-p", str(mount.root / prefix), str(clone)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
                           timeout=max(1, deadline - time.monotonic()))
            check()
        after = recovery_configuration(config.get("recoveryObjects", {}))
        if [o["metadata"]["resourceVersion"] for o in recovery] != [o["metadata"]["resourceVersion"] for o in after]:
            raise RuntimeError("recovery configuration changed during capture")
        # An in-flight GC can race only unreferenced artifacts. Every live
        # reference must still validate before any completion marker is written.
        selected = validate_fileset(clone, expected)
        file_list = work / "files.list"
        file_list.write_bytes(b"".join(p.encode() + b"\0" for p in selected))
        catalog = {"id": backup_id, "capturedUnix": time.time(), "configuration": config,
                   "artifacts": expected, "controlSchema": 1}
        (work / "catalog.json").write_text(json.dumps(catalog, sort_keys=True))
        (work / "recovery-config.json").write_text(json.dumps(recovery, sort_keys=True))
        bucket = oss_bucket(config["oss"])
        object_prefix = str(relative(config["oss"]["prefix"]))
        payload = f"{object_prefix}/payloads/{backup_id}"
        outputs = []
        for name in ("redis.rdb", "catalog.json", "recovery-config.json"):
            with (work / name).open("rb") as stream:
                outputs.append(upload(bucket, f"{payload}/{name}", stream, mount.check))
        outputs.append(upload_command(bucket, f"{payload}/postgres.dump",
                                      ["pg_dump", "--format=custom", "--no-owner", "--no-privileges"], mount.check))
        outputs.append(upload_command(bucket, f"{payload}/checkpoints.tar.gz", [
            "tar", "--create", "--gzip", "--sparse", "--format=pax", "--file=-",
            "--directory=" + str(clone), "--no-recursion", "--null", "--verbatim-files-from",
            "--files-from=" + str(file_list),
        ], mount.check))
        mount.check()
        manifest = {"version": 1, "id": backup_id, "completedUnix": time.time(),
                    "payloads": outputs, "artifactCount": len(expected)}
        bucket.put_object(f"{object_prefix}/manifests/{backup_id}.json",
                          json.dumps(manifest, sort_keys=True).encode(),
                          headers={"x-oss-forbid-overwrite": "true"})
        # Only a confirmed durable completion record authorizes local clone cleanup.
        mount.check()
        shutil.rmtree(clone)
        print(json.dumps({"result": "complete", "id": backup_id, "artifactCount": len(expected)}))
    finally:
        redis.close()
        # Failed clones remain pinned on JuiceFS for investigation/retry. EmptyDir
        # data is temporary and has never been the sole checkpoint copy.


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    try:
        run(json.loads(Path(args.config).read_text()))
    except Exception as error:
        # SDK errors can contain request metadata; never print credentials or bodies.
        print(json.dumps({"result": "failed", "errorType": type(error).__name__}), flush=True)
        raise SystemExit(1) from None
