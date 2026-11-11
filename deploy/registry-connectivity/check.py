"""Verify Registry authentication and Nydus reads from every Dragonfly seed.

Credentials are supplied to remote curl over exec stdin, never command arguments.
Only digests, sizes, timings and fixed diagnostic messages are recorded.
"""
from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import gzip
import io
import json
import os
import re
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urljoin

# A dedicated retained probe is tiny. Bound both transport and decompressed
# content instead of accepting arbitrary task-image bootstraps in this Job.
LIMIT = 1024 * 1024
CONCURRENCY = 2
MAX_REDIRECTS = 5
MARKER = b"\n__AKERNEL_HTTP_STATUS__"
EXIT_MARKER = b"\n__AKERNEL_CURL_EXIT__"
BOOTSTRAP = "containerd.io/snapshot/nydus-bootstrap"


class CheckFailed(RuntimeError):
    """A credential-free diagnostic safe for the Job log."""


def origin(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise CheckFailed("invalid endpoint")
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


def challenge(headers, allowed_origins):
    value = headers.get("www-authenticate", "")
    if not value.lower().startswith("bearer "):
        raise CheckFailed("Registry did not return a Bearer challenge")
    fields = dict(re.findall(r'(\w+)="([^"\r\n]*)"', value))
    if not fields.get("service") or origin(fields.get("realm", "")) not in {origin(u) for u in allowed_origins}:
        raise CheckFailed("token realm is not explicitly trusted")
    if urlsplit(fields["realm"]).query or urlsplit(fields["realm"]).fragment:
        raise CheckFailed("token realm must not contain query or fragment")
    return fields["realm"], fields["service"]


def decode_response(encoded):
    if len(encoded) > 2 * LIMIT:
        raise CheckFailed("seed response exceeds limit")
    try:
        raw = base64.b64decode(encoded, validate=False)
        response, exit_code = raw.rsplit(EXIT_MARKER, 1)
        if int(exit_code.strip()) != 0:
            raise CheckFailed("seed curl transfer failed")
        response, code = response.rsplit(MARKER, 1)
        status = int(code.strip())
        # curl may include proxy CONNECT / HTTP interim header blocks.
        while True:
            head, body = response.split(b"\r\n\r\n", 1)
            lines = head.decode("iso-8859-1").split("\r\n")
            if not lines[0].startswith("HTTP/"):
                raise ValueError("missing status")
            headers = {k.lower(): v.strip() for k, v in (line.split(":", 1) for line in lines[1:] if ":" in line)}
            if body.startswith(b"HTTP/") and ("connection established" in lines[0].lower() or lines[0].split()[1] == "100"):
                response = body
                continue
            break
        if status == 0 or len(body) > LIMIT or len(head) > 64 * 1024:
            raise ValueError("failed or too large")
        return status, headers, body
    except (ValueError, IndexError) as error:
        raise CheckFailed("seed HTTP request failed or returned an invalid response") from error


def curl_command(length):
    return ["/bin/sh", "-c", 'head -c "$1" | { curl --config - --silent --show-error --suppress-connect-headers --include --write-out "\\n__AKERNEL_HTTP_STATUS__%{http_code}\\n"; result=$?; printf "\\n__AKERNEL_CURL_EXIT__%s\\n" "$result"; } | base64', "registry-check", str(length)]


def digest_bytes(data, expected):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected or ""):
        raise CheckFailed("an immutable sha256 digest is required")
    if "sha256:" + hashlib.sha256(data).hexdigest() != expected:
        raise CheckFailed("content digest mismatch")


def bootstrap_descriptor(manifest):
    layers = manifest.get("layers", [])
    if not layers or BOOTSTRAP not in layers[-1].get("annotations", {}):
        raise CheckFailed("probe is not a Nydus manifest with a bootstrap layer")
    layer = layers[-1]
    if not isinstance(layer.get("size"), int) or not 0 < layer["size"] <= LIMIT:
        raise CheckFailed("bootstrap layer size is invalid")
    return layer


def bootstrap_contents(layer, descriptor):
    digest_bytes(layer, descriptor["digest"])
    if len(layer) != descriptor["size"]:
        raise CheckFailed("bootstrap layer size mismatch")
    try:
        # Limit decompression before tar parsing, including skipped members.
        if layer.startswith(b"\x1f\x8b"):
            with gzip.GzipFile(fileobj=io.BytesIO(layer)) as compressed:
                archive_data = compressed.read(2 * LIMIT + 1)
        else:
            archive_data = layer
        if len(archive_data) > 2 * LIMIT:
            raise CheckFailed("bootstrap archive exceeds decompression limit")
        with tarfile.open(fileobj=io.BytesIO(archive_data), mode="r:") as archive:
            member = archive.getmember("image/image.boot")
            if not member.isfile() or not 0 < member.size <= LIMIT:
                raise CheckFailed("bootstrap is not a bounded regular file")
            data = archive.extractfile(member).read(LIMIT + 1)
            if len(data) != member.size:
                raise CheckFailed("bootstrap file is truncated")
            return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    except (tarfile.TarError, KeyError, OSError, EOFError) as error:
        raise CheckFailed("cannot read image/image.boot from bootstrap layer") from error


class SeedHTTP:
    def __init__(self, api, namespace, pod, container, timeout=30):
        self.api, self.namespace, self.pod, self.container, self.timeout = api, namespace, pod, container, timeout

    def get(self, url, headers=None, proxy=None, ca_file=None):
        from kubernetes import client
        from kubernetes.stream import stream

        # Fixed-length stdin gives curl EOF without depending on a particular
        # Kubernetes exec websocket protocol's half-close implementation.
        options = {"url": url, "connect-timeout": "5", "max-time": str(self.timeout), "max-filesize": str(LIMIT), "noproxy": "" if proxy else "*", "proxy": proxy or ""}
        if proxy and urlsplit(proxy).scheme != "http":
            raise CheckFailed("Dragonfly probe requires an HTTP CONNECT proxy")
        if ca_file:
            options["cacert"] = ca_file
        config = "".join(f"{key} = {json.dumps(value)}\n" for key, value in options.items())
        config += "".join(f"header = {json.dumps(key + ': ' + value)}\n" for key, value in (headers or {}).items())
        payload = config.encode()
        command = curl_command(len(payload))
        # Kubernetes stream temporarily replaces ApiClient.request. A client
        # shared by concurrent seed checks would corrupt HTTP/exec dispatch.
        isolated = client.ApiClient(configuration=self.api.api_client.configuration)
        connection = None
        output = []
        size = 0
        deadline = time.monotonic() + self.timeout + 15
        try:
            connection = stream(client.CoreV1Api(isolated).connect_get_namespaced_pod_exec, self.pod, self.namespace, container=self.container, command=command, stdin=True, stdout=True, stderr=True, tty=False, _preload_content=False, _request_timeout=10)
            connection.write_stdin(config)
            while connection.is_open():
                if time.monotonic() >= deadline:
                    raise CheckFailed("seed exec deadline exceeded")
                connection.update(timeout=1)
                while connection.peek_stdout():
                    chunk = connection.read_stdout()
                    size += len(chunk)
                    if size > LIMIT * 2:
                        raise CheckFailed("seed response exceeds limit")
                    output.append(chunk)
                if connection.peek_stderr():
                    connection.read_stderr()  # Never expose upstream text/credentials.
            return decode_response("".join(output))
        finally:
            if connection is not None:
                connection.close()
            isolated.close()


def validate_probe_security(cfg):
    if cfg.get("proxyCheckEnabled", True):
        if cfg.get("proxyLoggingVerified") is not True:
            raise CheckFailed("proxy checks require verified credential-safe Dragonfly logging")
        if urlsplit(cfg["registry"]).scheme == "https" and not cfg.get("proxyCAFile"):
            raise CheckFailed("HTTPS proxy checks require the Dragonfly signing CA bundle path in each seed")


def read_bootstrap(http, cfg, url, headers, proxy=None):
    """Follow bounded blob redirects without forwarding cross-origin secrets.

    Each hop is a separate curl request: intermediate headers/bodies cannot be
    mistaken for the final layer. Credentials are never restored after leaving
    the initial origin, even if a later redirect returns to it.
    """
    initial_origin = origin(url)
    forwarded = dict(headers)
    visited = set()
    for hop in range(MAX_REDIRECTS + 1):
        if url in visited:
            raise CheckFailed("bootstrap redirect loop detected")
        visited.add(url)
        if proxy:
            ca_file = cfg.get("proxyCAFile")
            if urlsplit(url).scheme == "https" and not ca_file:
                raise CheckFailed("HTTPS proxy redirect requires the Dragonfly signing CA bundle")
        else:
            ca_file = cfg.get("registryCAFile") if origin(url) == initial_origin else cfg.get("redirectCAFile")
        status, response_headers, body = http.get(url, forwarded, proxy=proxy, ca_file=ca_file)
        if status == 200:
            return body
        if status not in (301, 302, 303, 307, 308) or not response_headers.get("location"):
            raise CheckFailed("authenticated bootstrap read failed")
        target = urljoin(url, response_headers["location"])
        target_origin = origin(target)  # HTTP(S) only, with no URL credentials.
        if urlsplit(url).scheme == "https" and urlsplit(target).scheme != "https":
            raise CheckFailed("bootstrap redirect would downgrade HTTPS")
        if origin(url) != target_origin:
            forwarded = {key: value for key, value in forwarded.items() if key.lower() not in ("authorization", "proxy-authorization", "cookie")}
        url = target
    raise CheckFailed("bootstrap redirect limit exceeded")


def verify_seed(http, cfg, credential):
    validate_probe_security(cfg)
    start = time.monotonic()
    registry = cfg["registry"].rstrip("/")
    status, headers, _ = http.get(registry + "/v2/", ca_file=cfg.get("registryCAFile"))
    if status != 401:
        raise CheckFailed("anonymous Registry access did not require authentication")
    realm, service = challenge(headers, cfg["allowedAuthOrigins"])
    scope = "repository:" + cfg["repository"] + ":pull"
    basic = base64.b64encode((credential["username"] + ":" + credential["password"]).encode()).decode()
    status, _, body = http.get(realm + "?" + urlencode({"service": service, "scope": scope}), {"Authorization": "Basic " + basic}, ca_file=cfg.get("authCAFile"))
    if status != 200:
        raise CheckFailed("pull token acquisition failed")
    try:
        token_data = json.loads(body)
        token = token_data.get("token") or token_data.get("access_token")
        if not isinstance(token, str) or not token:
            raise ValueError("missing token")
    except (ValueError, TypeError) as error:
        raise CheckFailed("invalid pull token response") from error
    auth = {"Authorization": "Bearer " + token, "Accept": "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"}
    root = registry + "/v2/" + cfg["repository"]
    status, _, body = http.get(root + "/manifests/" + cfg["digest"], auth, ca_file=cfg.get("registryCAFile"))
    if status != 200:
        raise CheckFailed("authenticated manifest read failed")
    digest_bytes(body, cfg["digest"])
    try:
        descriptor = bootstrap_descriptor(json.loads(body))
    except (ValueError, TypeError) as error:
        raise CheckFailed("invalid manifest JSON") from error
    url = root + "/blobs/" + descriptor["digest"]
    body = read_bootstrap(http, cfg, url, auth)
    result = bootstrap_contents(body, descriptor)
    proxy_checked = cfg.get("proxyCheckEnabled", True)
    if proxy_checked:
        body = read_bootstrap(http, cfg, url, auth, proxy=cfg.get("seedProxy", "http://127.0.0.1:4001"))
        if bootstrap_contents(body, descriptor) != result:
            raise CheckFailed("Dragonfly proxy bootstrap differs from Registry")
    return {"seed": http.pod, "passed": True, "proxy_checked": proxy_checked, "bootstrap": result, "layer_digest": descriptor["digest"], "seconds": round(time.monotonic() - start, 3)}


def ready(pod):
    return not pod.metadata.deletion_timestamp and any(c.type == "Ready" and c.status == "True" for c in (pod.status.conditions or []))


def fingerprint(api, apps, cfg):
    deployment = apps.read_namespaced_deployment(cfg["deployment"], cfg["namespace"], _request_timeout=20)
    spec, status = deployment.spec, deployment.status
    count = spec.replicas or 0
    if count < 1 or (status.observed_generation or 0) < deployment.metadata.generation or (status.updated_replicas or 0) != count or (status.available_replicas or 0) != count or (status.replicas or 0) != count:
        raise CheckFailed("Registry rollout is not ready")
    selector = ",".join(f"{k}={v}" for k, v in sorted(cfg["registrySelector"].items()))
    pods = [p for p in api.list_namespaced_pod(cfg["namespace"], label_selector=selector, _request_timeout=20).items if not p.metadata.deletion_timestamp]
    if len(pods) != count or not all(ready(p) for p in pods):
        raise CheckFailed("Registry Pods are not ready")
    selector = ",".join(f"{k}={v}" for k, v in sorted(cfg["seedSelector"].items()))
    seeds = api.list_namespaced_pod(cfg["seedNamespace"], label_selector=selector, _request_timeout=20).items
    selected = {p.metadata.name: p for p in seeds if p.metadata.name in cfg["seedNames"]}
    names = cfg["seedNames"]
    if not names or len(set(names)) != len(names) or set(selected) != set(names) or not all(ready(p) for p in selected.values()):
        raise CheckFailed("all configured Dragonfly seeds must be distinct, present and Ready")
    identities = {"deployment": [deployment.metadata.uid, deployment.metadata.generation], "registry": sorted((p.metadata.uid, [(s.name, s.image_id, s.restart_count) for s in (p.status.container_statuses or [])]) for p in pods), "seeds": sorted((name, p.metadata.uid, [(s.name, s.image_id, s.restart_count) for s in (p.status.container_statuses or [])]) for name, p in selected.items()), "config": cfg}
    return hashlib.sha256(json.dumps(identities, sort_keys=True).encode()).hexdigest()


def execute(api, apps, cfg, credential, state, force=False, now=None, http_factory=SeedHTTP):
    validate_probe_security(cfg)
    now = time.time() if now is None else now
    before = fingerprint(api, apps, cfg)
    if not force and state.get("fingerprint") == before and now - state.get("checked_epoch", 0) < cfg.get("recheckSeconds", 600):
        return None
    results = []
    def one(name):
        try:
            return verify_seed(http_factory(api, cfg["seedNamespace"], name, cfg["seedContainer"]), cfg, credential)
        except CheckFailed as error:
            return {"seed": name, "passed": False, "error": str(error)}
        except Exception:
            return {"seed": name, "passed": False, "error": "seed exec or HTTP transport failed"}
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        results = list(pool.map(one, cfg["seedNames"]))
    if not all(r["passed"] for r in results):
        print(json.dumps({"event": "verification_failed", "seeds": results}), flush=True)
        raise CheckFailed("one or more Dragonfly seeds failed; success state was not advanced")
    if len({r["bootstrap"]["sha256"] for r in results}) != 1:
        raise CheckFailed("seed bootstrap digests disagree")
    if fingerprint(api, apps, cfg) != before:
        raise CheckFailed("Registry or seed identity changed during validation; retry required")
    return {"fingerprint": before, "checked_epoch": now, "checked_at": datetime.fromtimestamp(now, timezone.utc).isoformat(), "manifest_digest": cfg["digest"], "seeds": results}


def main():
    from kubernetes import client, config
    config.load_incluster_config()
    api, apps = client.CoreV1Api(), client.AppsV1Api()
    cfg = json.loads(Path(os.environ.get("CONFIG_FILE", "/config/config.json")).read_text())
    credential = json.loads(Path("/credentials/credentials.json").read_text())
    cm = api.read_namespaced_config_map(cfg["stateConfigMap"], cfg["namespace"], _request_timeout=20)
    state = json.loads((cm.data or {}).get("state.json", "{}"))
    result = execute(api, apps, cfg, credential, state, force=os.environ.get("FORCE_CHECK") == "1")
    if result is None:
        print(json.dumps({"event": "unchanged", "last_success": state.get("checked_at")}), flush=True)
        return
    # resourceVersion prevents overlapping manual/hook Jobs overwriting state.
    api.patch_namespaced_config_map(cfg["stateConfigMap"], cfg["namespace"], {"metadata": {"resourceVersion": cm.metadata.resource_version}, "data": {"state.json": json.dumps(result, sort_keys=True)}}, field_manager="registry-connectivity-check", _request_timeout=20)
    print(json.dumps({"event": "verified", **result}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except CheckFailed as error:
        print(json.dumps({"event": "failed", "error": str(error)}), flush=True)
        raise SystemExit(1)
    except Exception:
        # Kubernetes/HTTP exceptions may include response bodies and credentials.
        print(json.dumps({"event": "failed", "error": "verification API or configuration error"}), flush=True)
        raise SystemExit(1)
