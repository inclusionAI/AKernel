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

"""Explain a scheduling snapshot without inferring retry or execution state."""

from collections.abc import Mapping
from dataclasses import dataclass

_RESOURCE_NAMES = {"cpu": "CPU", "memory": "memory", "disk": "disk"}
_PLACEMENT_HINT = "Relax placement constraints or add workers that satisfy them."
_OPERATOR_HINT = (
    "Inspect scheduling_failure with the cluster operator for remaining placement "
    "constraints."
)
_BLOCKERS = {
    "node_available": (
        "worker unavailable",
        "Ask the cluster operator to check worker health, registration and "
        "drain status.",
    ),
    "runtime_fit": (
        "unsupported runtime",
        "Select a supported runtime or add workers that advertise the "
        "requested runtime.",
    ),
    "device_fit": (
        "accelerator unavailable or incompatible",
        "Request an available accelerator model/count or add compatible workers.",
    ),
    "node_affinity": ("node affinity mismatch", _PLACEMENT_HINT),
    "placement_groups": ("placement group mismatch", _PLACEMENT_HINT),
    "environment_affinity": ("environment affinity mismatch", _PLACEMENT_HINT),
    "topology_spread": ("topology constraint", _PLACEMENT_HINT),
    "waiting_for_volume": (
        "volume in use",
        "Wait for the volume to be released or select a different volume.",
    ),
    "volume_topology": (
        "storage inaccessible from worker",
        "Select workers with access to the required storage or adjust "
        "volume placement.",
    ),
}


@dataclass(frozen=True)
class SchedulingExplanation:
    message: str
    reasons: tuple[str, ...]
    resource_shortages: tuple[str, ...] = ()
    suggestions: tuple[str, ...] = ()


def _count(value: object) -> int | None:
    # These are unsigned service counters/resources. Reject bool and malformed
    # values rather than coercing them into a diagnosis or masking the failure.
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2**64:
        return value
    return None


def _bytes(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB")
    for power, unit in reversed(tuple(enumerate(units))):
        scale = 1024**power
        if value >= scale:
            return f"{value / scale:g} {unit}"
    return "0 B"


def _requested(details: Mapping[str, object]) -> str:
    requested = details.get("requested")
    if not isinstance(requested, Mapping):
        return ""
    parts = []
    for key, resource in (
        ("cpu_millis", "CPU"),
        ("memory_bytes", "memory"),
        ("disk_bytes", "disk"),
    ):
        value = _count(requested.get(key))
        if value is not None:
            amount = f"{value / 1000:g}" if resource == "CPU" else _bytes(value)
            parts.append(f"{amount} {resource}")
    return " Requested: " + ", ".join(parts) + "." if parts else ""


def explain_scheduling_failure(details: object) -> SchedulingExplanation:
    """Use only explicit current-snapshot evidence; retain raw details elsewhere."""
    unknown = SchedulingExplanation(
        "the scheduling queue deadline expired without a usable placement diagnosis.",
        ("unknown",),
        suggestions=(_OPERATOR_HINT,),
    )
    if not isinstance(details, Mapping):
        return unknown
    eligible = _count(details.get("eligibleNodes"))
    nodes = _count(details.get("nodeCount"))
    if eligible is None or (nodes is not None and eligible > nodes):
        return unknown
    if eligible > 0:
        noun = "worker" if eligible == 1 else "workers"
        return SchedulingExplanation(
            f"the scheduling queue deadline expired with {eligible} eligible {noun}."
            + _requested(details),
            ("queue_timeout",),
            suggestions=(
                "Consider a larger schedule_timeout for a new create request and ask "
                "the cluster operator to check scheduler load.",
            ),
        )
    rejections = details.get("rejections")
    counts = (
        {
            key: count
            for key, value in rejections.items()
            if isinstance(key, str) and key and (count := _count(value))
        }
        if isinstance(rejections, Mapping)
        else {}
    )
    if nodes is not None and (
        any(count > nodes for key, count in counts.items() if key != "no_workers")
        or (nodes > 0 and "no_workers" in counts)
    ):
        return unknown
    if nodes == 0 or "no_workers" in counts:
        hint = "Ask the cluster operator to register healthy workers."
        return SchedulingExplanation(
            "the cluster has no workers available for scheduling."
            + _requested(details),
            ("no_workers",),
            suggestions=(hint,),
        )
    if not counts:
        return unknown
    resources = tuple(
        name for name in _RESOURCE_NAMES if f"insufficient_{name}" in counts
    )
    if resources:
        summary = (
            "the cluster has insufficient resources for this request; "
            "no worker satisfies all resource and placement requirements."
        )
    else:
        summary = (
            "no worker satisfies the current availability or placement requirements."
        )
    labels = []
    hints = []
    for resource in resources:
        key = f"insufficient_{resource}"
        name = _RESOURCE_NAMES[resource]
        labels.append(f"insufficient {name} ({counts[key]})")
        hints.append(
            f"Reduce the {name} request, release idle sandboxes, or add workers "
            f"with more available {name}."
        )
    for reason, count in sorted(counts.items()):
        if reason in {f"insufficient_{name}" for name in resources}:
            continue
        label, hint = _BLOCKERS.get(
            reason, ("other placement constraint", _OPERATOR_HINT)
        )
        labels.append(f"{label} ({count})")
        hints.append(hint)
    return SchedulingExplanation(
        summary
        + _requested(details)
        + " Worker rejections: "
        + ", ".join(labels)
        + ".",
        tuple(sorted(counts)),
        resources,
        tuple(dict.fromkeys(hints)),
    )
