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

"""Best-effort sandbox cleanup outside garbage-collection callbacks."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from queue import SimpleQueue
from threading import Lock, Thread

logger = logging.getLogger(__name__)
_queue: SimpleQueue[Callable[[], None]] = SimpleQueue()
_lock = Lock()
_worker: Thread | None = None


def _run(queue: SimpleQueue[Callable[[], None]]) -> None:
    while True:
        cleanup = queue.get()
        try:
            cleanup()
        except Exception:
            logger.warning("Deferred sandbox cleanup failed", exc_info=True)
        finally:
            # A waiting worker must not retain the last sandbox.
            del cleanup


def start_cleanup_worker() -> None:
    """Initialize the per-process worker during construction, never in GC."""
    global _worker
    with _lock:
        if _worker is None:
            _worker = Thread(
                target=_run, args=(_queue,), name="akernel-cleanup", daemon=True
            )
            _worker.start()


def defer_cleanup(cleanup: Callable[[], None]) -> None:
    """Queue cleanup without acquiring a Python lock or making a network call."""
    # CPython SimpleQueue.put is reentrant, including from __del__. In contrast,
    # Queue.put, thread creation and executor.submit can re-enter runtime locks.
    _queue.put(cleanup)


def _after_fork() -> None:
    global _queue, _lock, _worker
    # Parent threads do not survive fork. Do not execute parent cleanup in the
    # child; its next Sandbox construction starts a fresh worker and queue.
    _queue = SimpleQueue()
    _lock = Lock()
    _worker = None


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)
