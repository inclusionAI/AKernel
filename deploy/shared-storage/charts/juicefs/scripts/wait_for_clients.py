# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Bounded, read-only readiness gate for OnDelete JuiceFS mount clients."""

from __future__ import annotations

import json
import os
import ssl
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def pending(daemonset: dict, nodes: list[dict], pods: list[dict]) -> str:
    """Require the current client template to be ready on every consumer host."""
    hosts = {
        node["metadata"]["name"]
        for node in nodes
        if not node["metadata"].get("deletionTimestamp")
    }
    if not hosts:
        return "No nodes match the JuiceFS client/consumer selector"
    status = daemonset.get("status", {})
    if status.get("observedGeneration", 0) < daemonset["metadata"]["generation"]:
        return "Waiting for the DaemonSet controller to observe the current generation"
    desired = status.get("desiredNumberScheduled", 0)
    if desired != len(hosts):
        return f"Waiting for client coverage: desired={desired}, hosts={len(hosts)}"
    if status.get("updatedNumberScheduled", 0) != desired:
        return "Old OnDelete clients remain; drain consumers before replacing clients"
    ready = set()
    uid = daemonset["metadata"]["uid"]
    for pod in pods:
        metadata = pod["metadata"]
        owned = any(
            owner.get("uid") == uid and owner.get("controller") is True
            for owner in metadata.get("ownerReferences", [])
        )
        if (
            owned
            and not metadata.get("deletionTimestamp")
            and any(
                condition["type"] == "Ready" and condition["status"] == "True"
                for condition in pod.get("status", {}).get("conditions", [])
            )
        ):
            ready.add(pod["spec"].get("nodeName"))
    missing = hosts - ready
    if missing:
        return f"Waiting for ready JuiceFS mounts on: {', '.join(sorted(missing))}"
    if any(status.get(key, 0) != desired for key in ("numberReady", "numberAvailable")):
        return "Waiting for all current JuiceFS clients to become available"
    return ""


class Kubernetes:
    def __init__(self) -> None:
        service_account = Path("/var/run/secrets/kubernetes.io/serviceaccount")
        self.service_account = service_account
        self.context = ssl.create_default_context(
            cafile=str(service_account / "ca.crt")
        )
        self.base = "https://kubernetes.default.svc"

    def get(self, path: str) -> dict:
        # Read projected tokens for each request so rotation works during waits.
        token = (self.service_account / "token").read_text().strip()
        request = Request(
            self.base + path, headers={"Authorization": f"Bearer {token}"}
        )
        with urlopen(request, context=self.context, timeout=10) as response:
            return json.load(response)

    def items(self, path: str) -> list[dict]:
        result = []
        next_path = path
        while True:
            page = self.get(next_path)
            result.extend(page["items"])
            continuation = page.get("metadata", {}).get("continue")
            if not continuation:
                return result
            separator = "&" if "?" in path else "?"
            next_path = path + separator + urlencode({"continue": continuation})


def wait(check, timeout: float, *, now=time.monotonic, sleep=time.sleep) -> None:
    deadline = now() + timeout
    while True:
        reason = check()
        if not reason:
            print(
                "All JuiceFS consumer hosts have ready current mount clients",
                flush=True,
            )
            return
        print(reason, flush=True)
        remaining = deadline - now()
        if remaining <= 0:
            raise TimeoutError(f"JuiceFS mount readiness timed out: {reason}")
        sleep(min(5, remaining))


def main() -> None:
    client = Kubernetes()
    namespace = os.environ["POD_NAMESPACE"]
    name = os.environ["CLIENT_DAEMONSET"]

    def check() -> str:
        try:
            ds = client.get(f"/apis/apps/v1/namespaces/{namespace}/daemonsets/{name}")
            selector = ds["spec"]["template"]["spec"]["nodeSelector"]
            query = urlencode(
                {
                    "labelSelector": ",".join(
                        f"{k}={v}" for k, v in sorted(selector.items())
                    )
                }
            )
            nodes = client.items(f"/api/v1/nodes?{query}")
            pods = client.items(f"/api/v1/namespaces/{namespace}/pods")
            return pending(ds, nodes, pods)
        except (OSError, ValueError, KeyError) as error:
            # Do not print API response bodies or authorization headers.
            return f"Cannot verify JuiceFS mount readiness: {type(error).__name__}"

    wait(check, float(os.environ["READINESS_TIMEOUT_SECONDS"]))


if __name__ == "__main__":
    main()
