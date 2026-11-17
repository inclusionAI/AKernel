#!/usr/bin/env python3
"""Opt-in peer isolation test during maintenance; only source may initially schedule.

--manage-peer-scheduling temporarily admits the peer node, then blocks it again.
Only operator-owned guests are created; API/envd credentials never enter guests.
"""

import argparse
import json
import os
import shlex
import time
from pathlib import Path

from akernel_sdk import Client
from e2b import Sandbox
from qualification import IMAGE, fetch, placement


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection-file", required=True, type=Path)
    parser.add_argument("--ca", required=True, type=Path)
    parser.add_argument("--e2b-origin", help="optional E2B HTTP origin; native admin retains TLS")
    parser.add_argument("--source-node", required=True)
    parser.add_argument("--peer-node", required=True)
    parser.add_argument("--internal-entry", required=True)
    parser.add_argument("--manage-peer-scheduling", action="store_true", required=True)
    parser.add_argument("--result", required=True, type=Path)
    args = parser.parse_args()
    if args.result.exists():
        raise FileExistsError("fresh evidence path required")
    os.umask(0o077)
    os.environ["SSL_CERT_FILE"] = str(args.ca.resolve())
    config = json.loads(args.connection_file.read_text())
    native = Client(**config)
    options = {
        "api_key": config["api_key"],
        "api_url": args.e2b_origin or config["api_url"],
        "sandbox_url": args.e2b_origin or config["gateway_address"],
        "retries": 0,
        "request_timeout": 180,
    }
    headers = {
        "Authorization": "Bearer " + config["api_key"],
        "X-Auth": config["api_key"],
    }
    status, body = fetch(config["api_url"] + "/api/environment/v1/resources", headers)
    assert status == 200
    available = [x["id"] for x in json.loads(body)["items"] if x["status"] == 0]
    assert available == [args.source_node], (
        "only source must initially accept scheduling"
    )
    import ssl
    import urllib.parse
    import urllib.request

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )

    def schedule(method):
        url = (
            config["api_url"]
            + "/global-scheduler/node/localschedulingstatus?"
            + urllib.parse.urlencode({"node_id": args.peer_node})
        )
        with opener.open(
            urllib.request.Request(url, data=b"{}", method=method, headers=headers),
            timeout=20,
        ) as response:
            assert response.status == 200

    victim = None
    attackers = []
    admitted = False
    try:
        victim = Sandbox.create("base", timeout=600, **options)
        where = placement(config, victim.sandbox_id)
        assert where["node_id"] == args.source_node
        victim.files.write("/tmp/isolation-positive", "authenticated-envd")
        assert victim.files.read("/tmp/isolation-positive") == "authenticated-envd"
        observations = []
        for node, target in [
            (args.source_node, where["sandbox_ip"]),
            (args.peer_node, where["node_ip"]),
        ]:
            if node == args.peer_node:
                schedule("DELETE")
                admitted = True
                time.sleep(2)
            box = native.Sandbox.create(
                image=IMAGE,
                runtime="runsc",
                node_id=node,
                cpu=1000,
                memory=512,
                storage_mb=128,
                detached=True,
                timeout=300,
                init_timeout=120,
            )
            attackers.append(box)
            actual = placement(config, box.sandbox_id)
            assert actual["node_id"] == node
            code = (
                "import socket,json;s=socket.socket();s.settimeout(3);print(json.dumps({'connect_errno':s.connect_ex(("
                + repr(target)
                + ",49983))}));s.close()"
            )
            value = box.commands.run("python3 -c " + shlex.quote(code), timeout=10)
            assert value.exit_code == 0
            outcome = json.loads(value.stdout)
            assert outcome["connect_errno"] != 0
            observations.append(
                {"attacker": actual, "target": target, "port": 49983, **outcome}
            )
            if node == args.peer_node:
                box.files.write("/tmp/qualification-public-ca.pem", args.ca.read_text())
                code = """import ssl,urllib.request,urllib.error,json
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile='/tmp/qualification-public-ca.pem')))
try:
 with opener.open(urllib.request.Request(URL,headers=HEADERS),timeout=10) as r:status=r.status
except urllib.error.HTTPError as e:status=e.code
print(json.dumps({'status':status}))
""".replace(
                    "URL",
                    repr(args.internal_entry + "/files?path=/tmp/isolation-positive"),
                ).replace(
                    "HEADERS",
                    repr(
                        {
                            "E2b-Sandbox-Id": victim.sandbox_id,
                            "E2b-Sandbox-Port": "49983",
                        }
                    ),
                )
                value = box.commands.run("python3 -c " + shlex.quote(code), timeout=20)
                assert value.exit_code == 0
                denied = json.loads(value.stdout)["status"]
                assert denied in (401, 403)
        result = {
            "status": "validated",
            "victim": where,
            "tcp_checks": observations,
            "cross_node_gateway_without_token": denied,
            "authenticated_sdk_positive_control": True,
        }
    finally:
        errors = []
        for box in attackers:
            try:
                box.kill(delete_remote=True)
            except Exception as error:  # noqa: BLE001 - finish every owned cleanup
                errors.append(error)
        if victim:
            try:
                victim.kill()
            except Exception as error:  # noqa: BLE001 - still restore scheduling
                errors.append(error)
        if admitted:
            try:
                schedule("POST")
            except Exception as error:  # noqa: BLE001 - fail if peer fencing failed
                errors.append(error)
        if errors:
            raise RuntimeError(
                "owned guests or peer scheduling cleanup failed"
            ) from errors[0]
    result["status"] = "passed"
    args.result.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
