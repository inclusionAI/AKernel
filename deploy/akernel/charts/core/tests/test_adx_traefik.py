#!/usr/bin/env python3

"""Exercise the rendered SDK routes with the chart's real Traefik image.

Requires Helm, PyYAML, and a local Docker daemon. An intentionally unavailable
upstream makes 502 prove that a request matched a router; 404 means it did not.
This isolates routing from ADX readiness and requires no privileged node.
"""

from __future__ import annotations

import os
import ssl
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

import yaml
from test_adx_chart import render


class AdxTraefikRoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        resources, _ = render()
        dynamic = next(
            item["data"]["config.yml"]
            for item in resources
            if item["kind"] == "ConfigMap"
            and item["metadata"]["name"] == "traefik-dynamic"
        )
        deployment = next(
            item
            for item in resources
            if item["metadata"]["name"] == "traefik"
            and "template" in item.get("spec", {})
        )
        image = os.environ.get(
            "AKERNEL_TRAEFIK_TEST_IMAGE",
            deployment["spec"]["template"]["spec"]["containers"][0]["image"],
        )
        config = yaml.safe_load(dynamic)
        for service in config["http"]["services"].values():
            for server in service["loadBalancer"]["servers"]:
                # Keep the router, service selection, transport and protocol;
                # only substitute an unavailable endpoint for each upstream.
                scheme = urlsplit(server["url"]).scheme
                server["url"] = f"{scheme}://127.0.0.1:1"

        directory = tempfile.TemporaryDirectory(prefix="akernel-traefik-test-")
        cls.addClassCleanup(directory.cleanup)
        config_path = Path(directory.name) / "dynamic.yaml"
        config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
        config_path.chmod(0o644)
        cls.container = subprocess.check_output(
            [
                "docker",
                "run",
                "--detach",
                "--pull=missing",
                "--name",
                f"akernel-traefik-test-{uuid.uuid4().hex}",
                "--label",
                "akernel.test=traefik-routes",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--user",
                "65534:65534",
                "--publish",
                "127.0.0.1::8443",
                "--mount",
                f"type=bind,src={config_path},dst=/dynamic.yaml,readonly",
                image,
                "--entrypoints.websecure.address=:8443",
                "--entrypoints.web.address=:8080",
                "--providers.file.filename=/dynamic.yaml",
                "--providers.file.watch=false",
                "--global.checknewversion=false",
                "--global.sendanonymoususage=false",
            ],
            text=True,
        ).strip()
        cls.addClassCleanup(
            subprocess.run,
            ["docker", "rm", "--force", cls.container],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        endpoint = subprocess.check_output(
            ["docker", "port", cls.container, "8443/tcp"],
            text=True,
        ).strip()
        cls.base_url = f"https://{endpoint}"
        # Trust only this disposable loopback listener's default certificate.
        cls.opener = build_opener(
            ProxyHandler({}),
            HTTPSHandler(context=ssl._create_unverified_context()),
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if cls.status("/healthz") == 502:
                    return
            except (URLError, TimeoutError, ConnectionError):
                pass
            time.sleep(0.1)
        logs = subprocess.check_output(["docker", "logs", cls.container], text=True)
        raise RuntimeError(f"Traefik did not load the control router:\n{logs}")

    @classmethod
    def status(cls, path: str, *, websocket: bool = False) -> int:
        request = Request(cls.base_url + path)
        if websocket:
            request.add_header("Connection", "Upgrade")
            request.add_header("Upgrade", "websocket")
            request.add_header("Sec-WebSocket-Version", "13")
            request.add_header("Sec-WebSocket-Key", "dGhlIHNhbXBsZSBub25jZQ==")
        try:
            with cls.opener.open(request, timeout=2) as response:
                return response.status
        except HTTPError as error:
            return error.code

    def test_sdk_pty_websocket_reaches_control_ingress(self) -> None:
        self.assertEqual(
            self.status("/direct/instance-123/pty?tty=true", websocket=True),
            502,
        )

    def test_direct_root_reaches_control_ingress(self) -> None:
        self.assertEqual(self.status("/direct"), 502)

    def test_existing_control_routes_reach_ingress(self) -> None:
        for path in (
            "/",
            "/healthz",
            "/api/instances",
            "/api/sandbox",
            "/api/admin/v1/keys",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.status(path), 502)

    def test_direct_route_keeps_path_boundary(self) -> None:
        for path in ("/directly/instance-123/pty", "/unknown"):
            with self.subTest(path=path):
                self.assertEqual(self.status(path), 404)


if __name__ == "__main__":
    unittest.main()
