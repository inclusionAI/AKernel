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

"""Creation budgets shared by public validation and the supported adapter."""

DEFAULT_INIT_TIMEOUT = 30
# The supported ADX contract reserves this beyond scheduling and initialization.
CREATE_HANDOFF_SECONDS = 30
_MAX_SECONDS = (1 << 63) - 1


def validate_init_timeout(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("init_timeout must be an integer")
    if not 1 <= value <= 86400:
        raise ValueError("init_timeout must be between 1 and 86400 seconds")


def resolve_create_timeout(
    schedule_timeout: int,
    init_timeout: int,
    timeout: int | None,
    *,
    name: str = "create_timeout",
) -> int:
    """Derive an omitted budget; never widen an explicit caller limit."""
    validate_init_timeout(init_timeout)
    for key, value in (("schedule_timeout", schedule_timeout), (name, timeout)):
        if value is None and key == name:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{key} must be an integer")
        if not 1 <= value <= _MAX_SECONDS:
            raise ValueError(f"{key} must be between 1 and {_MAX_SECONDS} seconds")
    minimum = schedule_timeout + init_timeout + CREATE_HANDOFF_SECONDS
    if minimum > _MAX_SECONDS:
        raise ValueError("schedule_timeout + init_timeout + 30 exceeds backend range")
    if timeout is None:
        return minimum
    if timeout < minimum:
        raise ValueError(
            f"{name} must be at least schedule_timeout + init_timeout + 30 "
            f"({minimum} seconds)"
        )
    return timeout
