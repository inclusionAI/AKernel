# Copyright (c) 2026 Ant Group Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Query and parse the YuanRong cluster resource view."""

from __future__ import annotations

import json
import os
import ssl
from collections.abc import Mapping
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError

from ._addresses import api_endpoint_from_env
from .types import NodeInfo


class ResourceAPIError(RuntimeError):
    """An authenticated resource-view request failed."""

    def __init__(self, message: str, *, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


def _ssl_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def query_resource_view() -> dict[str, Any]:
    """Return the raw ``QueryResourcesInfoResponse`` JSON object."""

    token = os.environ.get("AKERNEL_TOKEN", "").strip()
    if not token:
        raise ResourceAPIError("AKERNEL_TOKEN is not set")
    endpoint = api_endpoint_from_env()
    req = request.Request(
        f"{endpoint.base_url()}/global-scheduler/resources",
        method="GET",
        headers={"X-Auth": token, "Type": "json"},
    )
    try:
        with request.urlopen(req, context=_ssl_context()) as response:
            body = response.read().decode("utf-8")
    except HTTPError as error:
        body = error.read().decode("utf-8")
        raise ResourceAPIError(
            f"server returned status {error.code}",
            status=error.code,
            body=body,
        ) from error
    except URLError as error:
        raise ResourceAPIError(f"failed to send request: {error}") from error

    try:
        value = json.loads(body)
    except json.JSONDecodeError as error:
        raise ResourceAPIError(
            f"failed to parse response: {error}", body=body
        ) from error
    if not isinstance(value, dict):
        raise ResourceAPIError("resource response is not a JSON object", body=body)
    return value


def _extract_scalar_value(value: Any) -> float:
    if isinstance(value, Mapping):
        return float(value.get("value", 0))
    return float(value)


def _extract_vector_count(resource: Mapping[str, Any]) -> float | None:
    vectors = resource.get("vectors")
    if not isinstance(vectors, Mapping):
        return None
    categories = vectors.get("values")
    if not isinstance(categories, Mapping):
        return None
    count = categories.get("count")
    if not isinstance(count, Mapping):
        return None
    grouped = count.get("vectors")
    if not isinstance(grouped, Mapping):
        return None

    total = 0.0
    found = False
    for vector in grouped.values():
        if not isinstance(vector, Mapping):
            continue
        values = vector.get("values")
        if not isinstance(values, list):
            continue
        for value in values:
            total += float(value)
            found = True
    return total if found else None


def extract_resources(proto_resources: Any) -> dict[str, float]:
    """Flatten scalar resources and XPU ``count`` vectors by resource name."""

    if not isinstance(proto_resources, Mapping):
        return {}
    inner = proto_resources.get("resources", proto_resources)
    if not isinstance(inner, Mapping):
        return {}

    result: dict[str, float] = {}
    for raw_name, resource in inner.items():
        name = str(raw_name)
        try:
            if isinstance(resource, Mapping):
                scalar = resource.get("scalar")
                if scalar is not None:
                    result[name] = _extract_scalar_value(scalar)
                    continue
                count = _extract_vector_count(resource)
                if count is not None:
                    result[name] = count
            elif isinstance(resource, (int, float, str)):
                result[name] = float(resource)
        except (TypeError, ValueError):
            continue
    return result


def extract_labels(proto_labels: Any) -> dict[str, list[str]]:
    """Convert protobuf-JSON counters to label value lists."""

    if not isinstance(proto_labels, Mapping):
        return {}
    result: dict[str, list[str]] = {}
    for raw_key, counter in proto_labels.items():
        items = counter.get("items", {}) if isinstance(counter, Mapping) else {}
        result[str(raw_key)] = (
            [str(item) for item in items] if isinstance(items, Mapping) else []
        )
    return result


def parse_resource_nodes(data: Mapping[str, Any]) -> list[NodeInfo]:
    """Convert a resource-view response into stable AKernel node values."""

    resource = data.get("resource", data)
    if not isinstance(resource, Mapping):
        return []
    fragment = resource.get("fragment")
    units = (
        list(fragment.values())
        if isinstance(fragment, Mapping) and fragment
        else [resource]
    )

    nodes: list[NodeInfo] = []
    for unit in units:
        if not isinstance(unit, Mapping):
            continue
        labels = extract_labels(unit.get("nodeLabels", unit.get("labels", {})))
        nodes.append(
            NodeInfo(
                id=str(unit.get("id", "")),
                status=int(unit.get("status", 0)),
                capacity=extract_resources(unit.get("capacity", {})),
                allocatable=extract_resources(unit.get("allocatable", {})),
                labels=labels,
            )
        )
    return nodes
