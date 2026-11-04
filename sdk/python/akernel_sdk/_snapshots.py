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

"""Backend-neutral reusable snapshot pagination."""

from __future__ import annotations

from collections.abc import Callable

from .types import SnapshotInfo


def snapshot_id(value: str | SnapshotInfo) -> str:
    if isinstance(value, SnapshotInfo):
        return value.snapshot_id
    if not isinstance(value, str) or not value.strip():
        raise ValueError("snapshot_id must be a non-empty string or SnapshotInfo")
    return value.strip()


def optional_name(value: str | None) -> str | None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError("name must be a non-empty string or None")
    return value.strip() if value is not None else None


class SnapshotPaginator:
    """Lazy snapshot pages, following E2B's ``has_next``/``next_items`` pattern.

    ``next_token`` can be saved and passed to ``Sandbox.list_snapshots`` to
    continue listing later. Failed requests leave the cursor unchanged; requests
    are not retried automatically. This object is not thread-safe.
    """

    def __init__(
        self,
        fetch: Callable[[str | None], tuple[list[SnapshotInfo], str | None]],
        next_token: str | None = None,
    ) -> None:
        self._fetch = fetch
        self._next_token = next_token or None
        self._has_next = True

    @property
    def has_next(self) -> bool:
        """Whether another page can be requested (including the first page)."""
        return self._has_next

    @property
    def next_token(self) -> str | None:
        """Cursor for the next request, or None before the first/after the last."""
        return self._next_token

    def next_items(self) -> list[SnapshotInfo]:
        """Fetch one page; return an empty list after the catalog is exhausted."""
        if not self._has_next:
            return []
        items, token = self._fetch(self._next_token)
        if token and token == self._next_token:
            raise RuntimeError("snapshot listing returned a non-advancing cursor")
        self._next_token = token or None
        self._has_next = bool(token)
        return items
