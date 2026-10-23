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

"""Public failure context for a fork whose resources need explicit cleanup."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .sandbox import Sandbox


class ForkCleanupError(RuntimeError):
    """A fork completed but some owned resource cleanup was not acknowledged.

    ``results`` retains every child result, including live Sandbox handles.
    Process/kill these handles as usual, and retry
    ``Sandbox.delete_snapshot(snapshot_id)``. The original structured backend
    error is available as ``cleanup_error`` and as the exception cause. Failed
    child bindings whose deletion failed remain in ``pending_sandboxes``;
    retry their ``kill()`` methods. ``child_cleanup_errors`` retains those errors.
    """

    def __init__(
        self,
        snapshot_id: str,
        results: list[Sandbox | Exception],
        cleanup_error: Exception,
        *,
        pending_sandboxes: tuple[Sandbox, ...] = (),
        child_cleanup_errors: tuple[Exception, ...] = (),
    ) -> None:
        super().__init__(
            f"fork resource cleanup failed (snapshot {snapshot_id!r}); "
            "child handles are available in results and pending_sandboxes"
        )
        self.snapshot_id = snapshot_id
        self.results = results
        self.cleanup_error = cleanup_error
        self.pending_sandboxes = pending_sandboxes
        self.child_cleanup_errors = child_cleanup_errors
