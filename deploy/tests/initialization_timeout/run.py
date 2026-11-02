# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0

"""Qualify initialization policy against an empty, dedicated runsc standalone.

The node-only fixture delays one matching runsc create during restore, never SDK calls.
Use a separately installed candidate wheel; this script does not install/import
the checkout SDK. The exclusive node must be destroyed after qualification.
"""

import argparse
import hashlib
import importlib.metadata
import json
import os
import shlex
import subprocess
import time
import uuid
from pathlib import Path

WRAPPER = r"""#!/usr/bin/python3
import json,os,sys,time
from pathlib import Path
root=Path('/home/akernel/initialization-qualification')
control=root/'armed.json'
if 'create' in sys.argv[1:] and control.exists():
    config=json.loads(control.read_text())
    bundle=Path(sys.argv[sys.argv.index('--bundle')+1])
    env=json.loads((bundle/'config.json').read_text())['process']['env']
    if 'ADX_ENVIRONMENT_ID='+config['environment_id'] in env:
        try:
            control.rename(root/'consumed.json')
        except FileNotFoundError:
            pass
        else:
            (root/'entered.json').write_text(json.dumps({
                'case':config['case'],'delay':config['delay'],
                'started':time.time(),'environment_id':config['environment_id']}))
            time.sleep(config['delay'])
os.execv('/usr/local/bin/runsc.qualification-real',
         ['/usr/local/bin/runsc.qualification-real',*sys.argv[1:]])
"""

CHECKPOINT = r"""import socket
connection=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
connection.settimeout(120)
connection.connect('/run/akernel/execd.sock')
connection.sendall(b'POST /checkpoint HTTP/1.1\r\nHost: localhost\r\n'
                   b'Content-Length: 0\r\nConnection: close\r\n\r\n')
response=b''.join(iter(lambda:connection.recv(65536),b''))
assert 200 <= int(response.split(b' ',2)[1]) < 300,response
assert b'"status":"completed"' in response,response
"""

WORKLOAD = r"""import time
from pathlib import Path
Path('/tmp/policy-marker').write_text('before')
Path('/tmp/policy-ready').write_text('ready')
while not Path('/tmp/finish-policy-job').exists():time.sleep(.1)
print('RESULT '+Path('/tmp/policy-nonce').read_text(),flush=True)
raise SystemExit(7)
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--container", default="akernel-node")
    parser.add_argument("--container-id", required=True)
    parser.add_argument("--allow-runtime-delay", action="store_true", required=True)
    parser.add_argument(
        "--image",
        required=True,
        help="Immutable Python rootfs image with shell and Python 3",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    data = args.data_dir.resolve()
    result = {
        "sdk": importlib.metadata.version("akernel-sdk"),
        "adx_sdk": importlib.metadata.version("adx-environment"),
        "cases": [],
        "status": "running",
    }

    def save():
        (output / "summary.json").write_text(json.dumps(result, indent=2) + "\n")

    def docker(*argv, stdin=None):
        return subprocess.run(
            ["docker", *argv],
            input=stdin,
            text=True,
            capture_output=True,
            check=True,
            timeout=45,
        ).stdout

    node = json.loads(docker("inspect", args.container))[0]
    if node["Id"] != args.container_id:
        raise RuntimeError("Node identity does not match --container-id")
    mounts = {m["Destination"]: m["Source"] for m in node["Mounts"]}
    if mounts.get("/home/akernel") != str(data):
        raise RuntimeError("Node data mount does not match --data-dir")
    node_id = node["Id"]
    result.update(node_id=node_id, image_id=node["Image"])
    redis_cli = "/opt/adx/current/bin/redis-cli"
    if docker("exec", node_id, redis_cli, "PING").strip() != "PONG":
        raise RuntimeError("Dedicated node Redis is unavailable")
    rpc_timeout = int(
        docker(
            "exec",
            node_id,
            "python3",
            "-c",
            "import yaml; from pathlib import Path; "
            "cfg=yaml.safe_load(Path('/etc/akernel/adx-standalone.yaml').read_text()); "
            "print(cfg.get('service_overrides',{}).get('adxlet',{}).get('config',{})"
            ".get('rpc_timeout_seconds',30))",
        ).strip()
    )
    result["node_rpc_timeout_seconds"] = rpc_timeout
    if rpc_timeout < 60:
        raise RuntimeError("Qualification needs node rpc_timeout_seconds >= 60")

    def endpoint(port, scheme):
        binding = node["NetworkSettings"]["Ports"][f"{port}/tcp"][0]
        if binding["HostIp"] not in ("127.0.0.1", "::1"):
            raise RuntimeError("Qualification requires loopback-published node ports")
        host = "[::1]" if binding["HostIp"] == "::1" else "127.0.0.1"
        return f"{scheme}://{host}:{binding['HostPort']}"

    os.environ["SSL_CERT_FILE"] = str(data / "adx/tls/ingress-public.pem")
    from akernel_sdk import Client

    client = Client(
        api_url=endpoint(8443, "https"),
        gateway_address=endpoint(8080, "http"),
        verify_tls=True,
        api_key=(data / "token").read_text().strip(),
    )
    Sandbox = client.Sandbox
    runtime = [
        "exec",
        node_id,
        "/usr/local/bin/runsc.qualification-real",
        "--root=/home/akernel/sandboxd/root/runsc",
    ]
    initial_runtime = [
        "exec",
        node_id,
        "runsc",
        "--root=/home/akernel/sandboxd/root/runsc",
    ]
    if json.loads(docker(*initial_runtime, "list", "--format=json")):
        raise RuntimeError("Qualification requires an empty physical runsc inventory")
    if Sandbox.list().next_items():
        raise RuntimeError("Qualification requires an empty logical inventory")
    fixture = data / "initialization-qualification"
    fixture.mkdir(exist_ok=False)
    wrapper = fixture / "runsc-wrapper.py"
    wrapper.write_text(WRAPPER)
    result["runtime_wrapper_sha256"] = hashlib.sha256(WRAPPER.encode()).hexdigest()
    # The full ID and exclusive mount were checked above. Fail rather than
    # overwrite a previous fixture; preserve every gVisor helper beside runsc.
    docker(
        "exec",
        node_id,
        "sh",
        "-ec",
        "test ! -e /usr/local/bin/runsc.qualification-real; "
        "mv /usr/local/bin/runsc /usr/local/bin/runsc.qualification-real; "
        "cp /home/akernel/initialization-qualification/runsc-wrapper.py "
        "/usr/local/bin/runsc; chmod 755 /usr/local/bin/runsc",
    )

    def record(identity):
        raw = docker(
            "exec",
            node_id,
            redis_cli,
            "--raw",
            "HGET",
            "adx:{akernel}:control:v1",
            f"environment:{identity}",
        ).strip()
        return json.loads(raw) if raw else None

    def await_condition(predicate, description, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.5)
        raise RuntimeError(f"Timed out waiting for {description}")

    def clean():
        return not json.loads(docker(*runtime, "list", "--format=json"))

    def arm(case, state):
        for filename in ("entered.json", "consumed.json"):
            (fixture / filename).unlink(missing_ok=True)
        control = {"case": case, "environment_id": state["spec"]["id"], "delay": 35}
        (fixture / "armed.json").write_text(json.dumps(control))

    def run_case(case, init_timeout, automatic=False):
        item = {"case": case, "init_timeout": init_timeout, "automatic": automatic}
        result["cases"].append(item)
        save()
        sandbox = None
        try:
            sandbox = Sandbox(
                image=args.image,
                cpu=1000,
                memory=1024,
                failover=True,
                init_timeout=init_timeout,
                create_timeout=180,
                idle_timeout=600,
            )
            item["environment_id"] = sandbox.id
            nonce = uuid.uuid4().hex
            sandbox.files.write("/tmp/policy-nonce", nonce)
            sandbox.files.write("/tmp/policy-job.py", WORKLOAD)
            handle = sandbox.commands.run(
                "python3 /tmp/policy-job.py", background=True, timeout=300
            )
            item.update(command_id=handle.command_id, pid=handle.pid)
            assert handle.command_id, "Background command has no stable identity"

            await_condition(
                lambda: (
                    sandbox.commands.run("test -f /tmp/policy-ready").exit_code == 0
                ),
                "original business command ready",
            )
            # The checkpoint caller itself is retired on restore. Keep it
            # separate from the original business command whose handle and
            # result must survive recovery.
            checkpoint = sandbox.commands.run(
                "python3 -c " + shlex.quote(CHECKPOINT), timeout=120
            )
            assert checkpoint.exit_code == 0, checkpoint

            def checkpoint_ready():
                state = record(sandbox.id)
                if not ((state or {}).get("result") or {}).get("checkpoint"):
                    return None
                return state

            state = await_condition(checkpoint_ready, "completed local checkpoint")
            assert state["spec"]["execution"]["init_timeout_seconds"] == init_timeout
            item["persisted_init_timeout"] = init_timeout
            sandbox.files.write("/tmp/policy-marker", "after")
            previous_runtime = state["result"]["runtime"]["id"]
            physical = json.loads(docker(*runtime, "list", "--format=json"))
            assert len(physical) == 1, "Expected exactly the owned runtime"
            item["physical_runtime_before"] = physical[0]["id"]
            arm(case, state)
            identity, commands, files = sandbox.id, sandbox.commands, sandbox.files
            started = time.monotonic()
            if automatic:
                docker(*runtime, "kill", "--all", physical[0]["id"], "KILL")

                def recovered():
                    current = record(identity)
                    view = (current or {}).get("result") or {}
                    return (
                        current
                        if view.get("state") == "Running"
                        and (view.get("runtime") or {}).get("id") != previous_runtime
                        else None
                    )

                await_condition(recovered, "automatic checkpoint failover", timeout=120)
                success = True
            else:
                success = sandbox.reload()
            item.update(elapsed=time.monotonic() - started, success=success)
            observed = (record(identity) or {}).get("result") or {}
            item["observed_state"] = observed.get("state")
            if observed.get("failure"):
                item["failure"] = observed["failure"]
            entered = json.loads((fixture / "entered.json").read_text())
            assert entered["case"] == case and entered["delay"] == 35
            item["delay_fixture_entered"] = True
            assert (sandbox.id, sandbox.commands, sandbox.files) == (
                identity,
                commands,
                files,
            )
            if init_timeout > 35:
                assert success and item["elapsed"] >= 35, item
                assert sandbox.files.read("/tmp/policy-marker") == "before"
                sandbox.files.write("/tmp/finish-policy-job", "finish")
                value = handle.wait(timeout=60)
                assert value.exit_code == 7 and value.stdout == f"RESULT {nonce}\n", (
                    value
                )
                item["original_command_result"] = {
                    "exit_code": value.exit_code,
                    "same_command_id": handle.command_id == item["command_id"],
                }
                assert sandbox.commands.run("printf subsequent").stdout == "subsequent"
            else:
                # The initialization deadline can settle before cleanup and
                # the synchronous caller return. Do not treat it as a bound
                # on total reload/cleanup wall time.
                assert not success, item
                failed = record(identity)
                failure = ((failed or {}).get("result") or {}).get("failure") or {}
                item["failure"] = failure
                assert failure.get("reason") == "DeadlineExceeded", failure
                assert "backend preparation/runtime start" in failure.get(
                    "message", ""
                ), failure
            item["status"] = "passed"
        except BaseException as error:
            item.update(status="failed", error=str(error))
            raise
        finally:
            if sandbox is not None:
                sandbox.kill(delete_remote=True)
                await_condition(clean, "physical cleanup", timeout=90)
                current = record(sandbox.id)
                held = ((current or {}).get("result") or {}).get(
                    "resources_held", False
                )
                assert not held, "Logical resource reservation remains held"
                item["cleanup"] = {"physical_empty": True, "resources_held": held}
            save()

    try:
        run_case("manual-long", 60)
        run_case("automatic-long", 60, automatic=True)
        run_case("manual-short", 10)
        # Wait beyond the injected delay to detect a late, canceled restore
        # reviving a physical sandbox after the failed attempt was deleted.
        time.sleep(36)
        assert clean(), "Late restore left a physical sandbox"
        assert not Sandbox.list().next_items(), "Logical inventory is not empty"
        result["status"] = "passed"
    except BaseException as error:
        result.update(status="failed", error=str(error))
        raise
    finally:
        result["final_physical_empty"] = clean()
        save()
        print(json.dumps({"status": result["status"], "output": str(output)}))


if __name__ == "__main__":
    main()
