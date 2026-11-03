#!/usr/bin/env python3
"""Opt-in real SDK qualification on one operator-owned deployment.

Use separately installed published SDKs; this probe never installs packages,
changes deployment settings, or prints credentials. Each phase owns its instances.
"""

import argparse
import json
import logging
import os
import shlex
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from functools import partial
from pathlib import Path

IMAGE = "docker.io/library/python@sha256:57d96f71de145a98e7ceca18ee4ade77bba4103a33a72dfaea026b23625c6bf0"
SERVER = """import json,secrets
from http.server import HTTPServer,BaseHTTPRequestHandler
nonce=secrets.token_hex(32);counter=41
class H(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_GET(self):
  self.send_response(200);self.end_headers();self.wfile.write(json.dumps(dict(nonce=nonce,counter=counter)).encode())
 def do_POST(self):
  global counter
  counter+=int(self.rfile.read(int(self.headers['Content-Length'])));self.do_GET()
HTTPServer(('0.0.0.0',8088),H).serve_forever()
"""


def command(box, text):
    result = box.commands.run(text, timeout=30)
    if result.exit_code:
        raise RuntimeError(
            "guest qualification command failed with exit " + str(result.exit_code)
        )
    return result.stdout


def memory(box, delta=None):
    text = "import urllib.request;"
    text += (
        'r=urllib.request.Request("http://127.0.0.1:8088/",data='
        + repr(None if delta is None else str(delta).encode())
        + ");"
    )
    text += "print(urllib.request.urlopen(r,timeout=5).read().decode())"
    return json.loads(command(box, "python3 -c " + shlex.quote(text)))


def start_workload(box, protocol):
    box.files.write("/tmp/dual-sdk-state.py", SERVER)
    box.files.write("/tmp/dual-sdk-file", "checkpoint-file")
    if protocol == "e2b":
        box.commands.run("python3 /tmp/dual-sdk-state.py", background=True, timeout=0)
    else:
        box.commands.run("python3 /tmp/dual-sdk-state.py", background=True)
    end = time.monotonic() + 30
    while True:
        try:
            return memory(box)
        except Exception:
            if time.monotonic() > end:
                raise
            time.sleep(0.2)


def fetch(url, headers):
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )
    try:
        with opener.open(
            urllib.request.Request(url, headers=headers), timeout=10
        ) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def placement(config, identity):
    status, body = fetch(
        config["api_url"] + "/api/instances?instance_id=" + identity,
        {"X-Auth": config["api_key"], "Type": "json"},
    )
    assert status == 200
    entry = json.loads(body)[0]
    return {
        key: entry[key] for key in ("node_id", "node_ip", "sandbox_ip", "runtime_id")
    }


def wait_delete_snapshot(delete):
    deadline = time.monotonic() + 60
    while True:
        try:
            delete()
            return
        except Exception as error:
            if "in use" not in str(error).lower() or time.monotonic() > deadline:
                raise
            time.sleep(0.5)


def main():
    logging.basicConfig(level=logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["native", "e2b", "native-logs", "e2b-logs"])
    parser.add_argument("--connection-file", type=Path, required=True)
    parser.add_argument("--ca", type=Path, required=True)
    parser.add_argument("--other-key-file", type=Path)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--image", default=IMAGE)
    parser.add_argument("--template", default="base")
    parser.add_argument("--source-node")
    parser.add_argument("--target-node")
    parser.add_argument("--hold-seconds", type=int, default=0)
    parser.add_argument(
        "--http-port-entry",
        help="existing HTTP data listener, without control credentials",
    )
    args = parser.parse_args()
    if args.phase == "e2b" and not args.other_key_file:
        parser.error("E2B qualification requires a separately owned foreign-tenant key")
    if args.result.exists():
        raise FileExistsError("fresh evidence path required")
    os.umask(0o077)
    os.environ["SSL_CERT_FILE"] = str(args.ca.resolve())
    config = json.loads(args.connection_file.read_text())
    owned = []
    snapshots = []
    checks = []
    result = {"phase": args.phase, "status": "running", "checks": checks}
    from akernel_sdk import Client, Mount

    native = Client(**config)
    e2b_options = {
        "api_key": config["api_key"],
        "api_url": config["api_url"],
        "sandbox_url": config["gateway_address"],
        "retries": 0,
        "request_timeout": 180,
        "debug": False,
    }

    def save():
        args.result.write_text(json.dumps(result, indent=2) + "\n")

    def native_create(**kwargs):
        box = native.Sandbox.create(
            image=args.image,
            runtime="runsc",
            cpu=1000,
            memory=1024,
            storage_mb=512,
            detached=True,
            timeout=300,
            init_timeout=120,
            **kwargs,
        )
        owned.append(("native", box))
        return box

    def e2b_create(reference=None):
        from e2b import Sandbox

        box = Sandbox.create(reference or args.template, timeout=600, **e2b_options)
        owned.append(("e2b", box))
        return box

    try:
        if args.phase == "native-logs":
            marker = "dual-native-" + uuid.uuid4().hex + ".log"
            path = "/app/logs/" + marker
            writable = [
                Mount(host_path="/app/logs", target="/app/logs", read_only=False)
            ]
            rw = native_create(node_id=args.source_node, mounts=writable)
            uid = command(rw, "id -u").strip()
            command(rw, "test ! -e /home/akernel/sandboxd/config/registry_auths.json")
            result["placement"] = placement(config, rw.sandbox_id)
            command(rw, "umask 0007; printf durable > " + shlex.quote(path))
            assert rw.files.read(path) == "durable"
            readonly = [Mount(host_path="/app/logs", target="/app/logs")]
            ro = native_create(node_id=args.source_node, mounts=readonly)
            assert ro.files.read(path) == "durable"
            assert (
                ro.commands.run("printf forbidden >> " + shlex.quote(path)).exit_code
                != 0
            )
            for box, mounts in [(rw, writable), (ro, readonly)]:
                snapshot = box.create_snapshot(name="dual-log-" + uuid.uuid4().hex)
                snapshots.append(("native", snapshot.snapshot_id))
                restored = native.Sandbox.from_snapshot(
                    snapshot.snapshot_id,
                    mounts=mounts,
                    node_id=args.source_node,
                    detached=True,
                    timeout=300,
                )
                owned.append(("native", restored))
                assert restored.files.read(path).startswith("durable")
                written = restored.commands.run(
                    "printf restored >> " + shlex.quote(path)
                ).exit_code
                assert written != 0 if mounts[0].read_only else written == 0
            result.update(log_file=path, writer_uid=uid, node=args.source_node)
            checks.extend(
                [
                    "actual image create",
                    "explicit /app/logs RW",
                    "explicit default RO denies writes",
                    "explicit RO/RW checkpoint restore preserves access mode",
                ]
            )
        elif args.phase == "e2b-logs":
            box = e2b_create()
            path = "/app/logs/dual-e2b-" + uuid.uuid4().hex + ".log"
            uid = command(box, "id -u").strip()
            command(box, "test ! -e /home/akernel/sandboxd/config/registry_auths.json")
            result["placement"] = placement(config, box.sandbox_id)
            assert uid != "0"
            command(box, "umask 0007; printf durable > " + shlex.quote(path))
            assert box.files.read(path) == "durable"
            result.update(log_file=path, writer_uid=uid, sandbox_id=box.sandbox_id)
            checks.extend(
                [
                    "actual template image create",
                    "E2B default non-root user",
                    "deployment-owned /app/logs RW",
                ]
            )
        else:
            protocol = args.phase
            if protocol == "native":
                source = native_create(
                    node_id=args.source_node, port_forwardings=[8088]
                )
                identity = source.sandbox_id
            else:
                source = e2b_create()
                identity = source.sandbox_id
            result["created_placement"] = placement(config, identity)
            original = start_workload(source, protocol)
            checks.extend(["create", "commands", "files", "process memory state"])
            output = []
            if protocol == "native":
                with source.pty.create(on_data=output.append) as session:
                    session.send_stdin(b"printf dual-pty-ok; exit 7\n")
                    assert session.wait(timeout=30) == 7
                assert b"dual-pty-ok" in b"".join(output)
                status, body = fetch(source.get_port_url(8088) + "/", {})
            else:
                from e2b import PtySize, Sandbox

                terminal = source.pty.create(PtySize(cols=80, rows=24), timeout=30)
                source.pty.send_stdin(terminal.pid, b"printf dual-pty-ok; exit 7\n")
                from e2b import CommandExitException

                try:
                    terminal.wait(on_pty=output.append)
                except CommandExitException as error:
                    assert error.exit_code == 7
                else:
                    raise AssertionError("PTY exit7 was not returned")
                assert b"dual-pty-ok" in b"".join(output)
                routing = {"E2b-Sandbox-Id": identity, "E2b-Sandbox-Port": "8088"}
                status, body = fetch(config["gateway_address"] + "/", routing)
                # This deployment uses the supported fixed HTTPS entry, not wildcard DNS.
                assert identity in source.get_host(8088)
                forbidden = dict(routing)
                forbidden["E2b-Sandbox-Port"] = "49983"
                no_token, _ = fetch(
                    config["gateway_address"] + "/files?path=/tmp/dual-sdk-file",
                    forbidden,
                )
                invalid, _ = fetch(
                    config["api_url"] + "/sandboxes/" + identity,
                    {"X-API-Key": "invalid-qualification-key"},
                )
                wrong_token, _ = fetch(
                    config["gateway_address"] + "/files?path=/tmp/dual-sdk-file",
                    {**forbidden, "X-Access-Token": "invalid-envd-token"},
                )
                assert (
                    no_token in (401, 403)
                    and wrong_token in (401, 403)
                    and invalid == 401
                )
                if args.other_key_file:
                    foreign, _ = fetch(
                        config["api_url"] + "/sandboxes/" + identity,
                        {"X-API-Key": args.other_key_file.read_text().strip()},
                    )
                    assert foreign in (403, 404)
                checks.extend(
                    [
                        "envd missing-token denied",
                        "invalid API key denied",
                        "foreign tenant denied",
                    ]
                )
            assert status == 200 and json.loads(body) == original
            if args.http_port_entry:
                entry = args.http_port_entry.rstrip("/")
                assert urllib.parse.urlsplit(entry).scheme == "http"
                if protocol == "native":
                    parsed = urllib.parse.urlsplit(source.get_port_url(8088))
                    suffix = parsed.path + ("?" + parsed.query if parsed.query else "")
                    port_status, port_body = fetch(entry + suffix, {})
                else:
                    port_status, port_body = fetch(entry + "/", routing)
                result["http_data_status"] = port_status
                if protocol == "native":
                    assert port_status == 200 and json.loads(port_body) == original
                    checks.append("HTTP80 application port")
                else:
                    # ADX proxy_e2b deliberately requires TLS for every E2B route.
                    assert port_status == 426
                    checks.append("E2B HTTP80 route rejects plaintext")
                control_status, _ = fetch(entry + "/api/environment/v1/resources", {})
                assert control_status == 426
                checks.append("HTTP80 control route rejects plaintext")
            checks.extend(["PTY exit7", "published HTTP port"])
            snapshot = source.create_snapshot(name="dual-sdk-" + uuid.uuid4().hex)
            sid = snapshot.snapshot_id
            snapshots.append((protocol, sid))
            assert memory(source) == original
            assert source.pause()
            if protocol == "native":
                source.close()
                source = native.Sandbox.connect(identity, timeout=300)
                owned.append(("native", source))
            else:
                source = Sandbox.connect(identity, timeout=600, **e2b_options)
                owned.append(("e2b", source))
            result["resumed_placement"] = placement(config, identity)
            assert source.sandbox_id == identity and memory(source) == original
            assert source.files.read("/tmp/dual-sdk-file") == "checkpoint-file"
            checks.extend(
                [
                    "leave-running Snapshot",
                    "pause",
                    "same-ID resume with memory/file state",
                ]
            )
            if protocol == "native":
                clone = native.Sandbox.from_snapshot(
                    sid, node_id=args.target_node, detached=True, timeout=300
                )
                owned.append(("native", clone))
            else:
                clone = e2b_create(sid)
            result["clone_placement"] = placement(config, clone.sandbox_id)
            if args.target_node:
                assert result["clone_placement"]["node_id"] == args.target_node
            assert memory(clone) == original
            assert (
                memory(source, 11)["counter"] == 52
                and memory(clone, 7)["counter"] == 48
            )
            source.files.write("/tmp/dual-sdk-file", "source-only")
            clone.files.write("/tmp/dual-sdk-file", "clone-only")
            assert source.files.read("/tmp/dual-sdk-file") == "source-only"
            assert clone.files.read("/tmp/dual-sdk-file") == "clone-only"
            if protocol == "e2b":
                correct_headers = clone.connection_config.sandbox_headers
                source_headers = source.connection_config.sandbox_headers
                assert (
                    correct_headers["X-Access-Token"]
                    != source_headers["X-Access-Token"]
                )
                path = config["gateway_address"] + "/files?path=/tmp/dual-sdk-file"
                positive, data = fetch(path, correct_headers)
                assert positive == 200 and data.decode() == "clone-only"
                foreign_headers = {
                    **correct_headers,
                    "X-Access-Token": source_headers["X-Access-Token"],
                }
                rejected, _ = fetch(path, foreign_headers)
                assert rejected in (401, 403)
                checks.append("other sandbox envd token denied with positive control")
            checks.extend(
                ["snapshot clone restored memory", "concurrent private memory/files"]
            )
            result.update(
                source_id=identity,
                clone_id=clone.sandbox_id,
                snapshot_id=sid,
                source_requested=args.source_node,
                target_requested=args.target_node,
                status="validated",
            )
            save()
            if args.hold_seconds:
                time.sleep(args.hold_seconds)
        result["status"] = "validated"
        save()
    except BaseException as error:
        result.update(status="failed", error_type=type(error).__name__)
        save()
        raise
    finally:
        errors = []
        deleted = set()
        for protocol, box in reversed(owned):
            identity = box.sandbox_id
            if identity in deleted:
                continue
            try:
                if protocol == "native":
                    native.Sandbox.kill(identity)
                    box.close()
                else:
                    box.kill()
                deleted.add(identity)
            except Exception as error:  # noqa: BLE001 - finish every owned cleanup and fail the run
                errors.append(type(error).__name__)
        for protocol, identity in snapshots:
            try:
                if protocol == "native":
                    wait_delete_snapshot(
                        partial(native.Sandbox.delete_snapshot, identity)
                    )
                else:
                    from e2b import Sandbox

                    wait_delete_snapshot(
                        partial(Sandbox.delete_snapshot, identity, **e2b_options)
                    )
            except Exception as error:  # noqa: BLE001 - finish every owned cleanup and fail the run
                errors.append(type(error).__name__)
        result["deleted_instances"] = sorted(deleted)
        result["cleanup_errors"] = errors
        if errors:
            result["status"] = "failed"
        elif result["status"] == "validated":
            result["status"] = "passed"
        save()
        if errors:
            raise RuntimeError("owned resource cleanup failed: " + ",".join(errors))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
