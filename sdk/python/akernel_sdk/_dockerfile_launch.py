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

"""Lightweight public configuration for Dockerfile direct launch."""

from __future__ import annotations

from dataclasses import dataclass

from ._dockercontext import DockerContext


@dataclass(frozen=True)
class DockerfileLaunch:
    """Immutable supported configuration for Dockerfile direct launch.

    Dockerfile direct launch accepts the documented strict subset. Unsupported
    inputs fail closed.

    Args:
        context: Dockerfile and build context to apply in the sandbox.
        auto_start_cmd: Dispatch the Dockerfile CMD/ENTRYPOINT after applying
            build-time instructions. Defaults to ``True``.
        run_timeout: Positive per-``RUN`` timeout in seconds. Defaults to
            ``600``.
    """

    context: DockerContext
    auto_start_cmd: bool = True
    run_timeout: int = 600

    def __post_init__(self) -> None:
        if not isinstance(self.context, DockerContext):
            raise TypeError("context must be a DockerContext")
        if not isinstance(self.auto_start_cmd, bool):
            raise TypeError("auto_start_cmd must be a boolean")
        if isinstance(self.run_timeout, bool) or not isinstance(
            self.run_timeout, int
        ):
            raise TypeError("run_timeout must be an integer")
        if self.run_timeout <= 0:
            raise ValueError("run_timeout must be greater than zero")
