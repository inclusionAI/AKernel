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

"""Validation and wire translation for sandbox accelerator and storage requests."""

from __future__ import annotations

import re

_XPU_MODEL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_XPU_COUNT_PATTERN = re.compile(r"^[0-9]+$")
_MIB = 1024 * 1024
_MAX_EXACT_DOUBLE_INTEGER = (1 << 53) - 1
MAX_STORAGE_MB = _MAX_EXACT_DOUBLE_INTEGER // _MIB


def normalize_xpu(value: str | None) -> str | None:
    """Validate and canonicalize a ``type:model:count`` XPU request."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("xpu must be a string")

    fields = value.split(":")
    if len(fields) != 3:
        raise ValueError("xpu must contain exactly three fields: type:model:count")
    xpu_type, model, count_text = (field.strip().lower() for field in fields)
    if xpu_type != "gpu":
        raise ValueError("xpu type must be gpu")
    if not model:
        raise ValueError("xpu model must be non-empty")
    if not _XPU_MODEL_PATTERN.fullmatch(model):
        raise ValueError(
            "xpu model may contain only lowercase letters, digits, '.', '_' and '-'"
        )
    if not _XPU_COUNT_PATTERN.fullmatch(count_text) or int(count_text) <= 0:
        raise ValueError("xpu count must be a positive integer")
    return f"{xpu_type}:{model}:{int(count_text)}"


def xpu_custom_resource(value: str) -> tuple[str, float]:
    """Return the exact-model YuanRong custom-resource key and count."""

    normalized = normalize_xpu(value)
    assert normalized is not None
    xpu_type, model, count_text = normalized.split(":")
    return f"{xpu_type.upper()}/{re.escape(model)}/count", float(count_text)


def _validate_storage_value(name: str, value: int | None) -> None:
    """Validate one MiB storage value accepted by YuanRong's scalar wire type."""

    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value <= 0:
        raise ValueError(f"{name} must be greater than 0")
    if value > MAX_STORAGE_MB:
        raise ValueError(f"{name} must not exceed {MAX_STORAGE_MB}")


def validate_storage_mb(value: int | None) -> None:
    """Validate a writable-layer scheduling request in MiB."""

    _validate_storage_value("storage_mb", value)


def validate_storage(
    storage_mb: int | None,
    storage_limit_mb: int | None,
) -> None:
    """Validate writable-layer request and hard-limit values in MiB."""

    validate_storage_mb(storage_mb)
    _validate_storage_value("storage_limit_mb", storage_limit_mb)
    if (
        storage_mb is not None
        and storage_limit_mb is not None
        and storage_limit_mb < storage_mb
    ):
        raise ValueError("storage_limit_mb must be greater than or equal to storage_mb")


def storage_bytes(value: int) -> float:
    """Convert a validated positive MiB value to YuanRong's byte scalar."""

    validate_storage_mb(value)
    return float(value * _MIB)
