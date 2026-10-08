"""Temporary tenant API keys for explicitly enabled full-deployment tests."""

import json
import os
import ssl
import urllib.request
import uuid

from akernel_sdk._addresses import api_endpoint_from_env


def management_enabled() -> bool:
    return os.environ.get("AKERNEL_TEST_MANAGE_KEYS") == "1"


def _request(method: str, path: str, body: dict | None = None) -> dict:
    endpoint = api_endpoint_from_env()
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        endpoint.base_url() + path,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + os.environ["AKERNEL_TOKEN"],
            "Content-Type": "application/json",
        },
    )
    context = None
    if endpoint.use_tls:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(request, timeout=20, context=context) as response:
        payload = response.read()
        return json.loads(payload) if payload else {}


def create_key(*, expires_at: int = 0) -> tuple[str, str]:
    tenant = "e2e-" + uuid.uuid4().hex[:16]
    result = _request(
        "POST",
        "/api/admin/v1/keys",
        {"tenantId": tenant, "expiresAtUnixSeconds": expires_at},
    )
    key_id = result["key"]["id"]
    token = result["apiKey"]
    if not key_id or len(token) < 32:
        raise ValueError("key creation returned an invalid credential")
    return key_id, token


def revoke_key(key_id: str) -> None:
    _request("DELETE", f"/api/admin/v1/keys/{key_id}")
