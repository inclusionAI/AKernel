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

"""Backend-neutral sandbox queries and lazy pagination."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum

from .types import SandboxInfo


class SandboxState(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"
    PENDING = "pending"
    STARTING = "starting"
    PAUSING = "pausing"
    RESUMING = "resuming"
    DELETING = "deleting"
    FAILED = "failed"


@dataclass(frozen=True)
class SandboxQuery:
    """Filter visible sandboxes; no state filter includes running and paused."""

    state: Sequence[SandboxState | str] | None = None

    def __post_init__(self) -> None:
        values = self.state
        if values is None:
            values = (SandboxState.RUNNING, SandboxState.PAUSED)
        if isinstance(values, (str, bytes)):
            raise TypeError("state must be a sequence of SandboxState values")
        object.__setattr__(self, "state", tuple(SandboxState(v) for v in values))


def sandbox_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("sandbox_id must be a non-empty string")
    return value.strip()


class SandboxPaginator:
    """Lazy pages; errors preserve the cursor for an explicit retry."""

    def __init__(
        self,
        fetch: Callable[[str | None], tuple[list[SandboxInfo], str | None]],
        next_token: str | None = None,
    ) -> None:
        self._fetch = fetch
        self._next_token = next_token or None
        self._has_next = True

    @property
    def has_next(self) -> bool:
        return self._has_next

    @property
    def next_token(self) -> str | None:
        return self._next_token

    def next_items(self) -> list[SandboxInfo]:
        if not self._has_next:
            return []
        items, token = self._fetch(self._next_token)
        if token is not None and token == self._next_token:
            raise RuntimeError("backend returned a non-advancing sandbox cursor")
        self._next_token = token
        self._has_next = token is not None
        return items
