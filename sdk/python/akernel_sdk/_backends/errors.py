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

"""Stable errors raised at the AKernel backend boundary."""

from typing import TYPE_CHECKING, Any

from ._scheduling import explain_scheduling_failure

if TYPE_CHECKING:
    from ..types import CommandResult


class InvalidBackendError(ValueError):
    """The configured backend identifier is not supported."""


class BackendNotInstalledError(ImportError):
    """The selected backend dependency is not installed."""


class UnsupportedBackendFeatureError(ValueError):
    """The selected backend cannot preserve a requested AKernel feature."""


class BackendOperationError(RuntimeError):
    """A backend operation failed, with optional service error context.

    Missing fields are ``None``. ``retry`` and ``outcome`` retain the service's
    instructions; callers must not infer execution state from the message or
    an HTTP status. The original backend error remains available as the cause.
    ``cleanup_errors`` retains rollback failures without replacing that cause.
    ``first_error`` retains the native client's earliest failed attempt without
    changing the final error's retry/outcome guidance.
    ``command_result`` retains available command output on an execution/wait
    timeout without implying that the remote command reached a terminal exit.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retry: str | None = None,
        outcome: str | None = None,
        request_id: str | None = None,
        operation_id: str | None = None,
        environment_id: str | None = None,
        status_code: int | None = None,
        scheduling_failure: dict[str, Any] | None = None,
        failure: dict[str, Any] | None = None,
        probe_failure: dict[str, Any] | None = None,
        request_forwarded: bool | None = None,
        cleanup_errors: tuple[BaseException, ...] = (),
        command_id: str | None = None,
        first_error: BaseException | None = None,
        command_result: "CommandResult | None" = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retry = retry
        self.outcome = outcome
        self.request_id = request_id
        self.operation_id = operation_id
        self.environment_id = environment_id
        self.status_code = status_code
        self.scheduling_failure = scheduling_failure
        self.failure = failure
        self.probe_failure = probe_failure
        self.request_forwarded = request_forwarded
        self.cleanup_errors = cleanup_errors
        self.command_id = command_id
        self.first_error = first_error
        self.command_result = command_result


class AuthenticationException(BackendOperationError):
    """The cluster rejected the caller's authentication."""


class PermissionDeniedException(BackendOperationError):
    """The identity is authenticated but cannot perform the operation."""


class NotFoundException(BackendOperationError):
    """The requested resource does not exist or is not visible."""


class SandboxNotFoundException(NotFoundException):
    """The requested sandbox does not exist or is not visible."""


class TimeoutException(BackendOperationError):
    """An operation or command wait exceeded its declared timeout."""


class RateLimitException(BackendOperationError):
    """The service rejected work because of a caller or capacity limit."""


class ServiceBusyException(BackendOperationError):
    """The service is temporarily unavailable; inspect retry/outcome context."""


class SchedulingError(BackendOperationError):
    """A service-confirmed scheduling cancellation with placement diagnostics.

    ``reasons`` contains positive rejection keys when no worker is eligible,
    ``queue_timeout`` when workers are eligible, or ``unknown`` for unusable
    diagnostics. ``resource_shortages`` lists contributing CPU/memory/disk
    dimensions; other constraints may also contribute. ``suggestions`` offers
    recovery advice without authorizing a retry or changing service context.
    Original diagnostics remain in ``scheduling_failure`` and the native error
    in ``__cause__``. Rejection counts can overlap and are not a node total.
    """

    @property
    def reasons(self) -> tuple[str, ...]:
        """Reported rejection keys, or the queue_timeout/unknown classification."""
        return explain_scheduling_failure(self.scheduling_failure).reasons

    @property
    def resource_shortages(self) -> tuple[str, ...]:
        """Contributing resources: cpu, memory and/or disk; empty when unconfirmed."""
        return explain_scheduling_failure(self.scheduling_failure).resource_shortages

    @property
    def suggestions(self) -> tuple[str, ...]:
        """Actions to consider before deliberately creating a new request."""
        return explain_scheduling_failure(self.scheduling_failure).suggestions


class InsufficientResourcesError(SchedulingError):
    """No worker is eligible and CPU, memory or disk shortages contribute.

    Runtime, placement or volume constraints may also contribute; inspect
    ``reasons`` and ``suggestions`` rather than assuming resources are the
    sole blocker. The snapshot does not predict capacity after scaling.
    """


class CommandExitException(BackendOperationError):
    """An explicitly checked command exited unsuccessfully."""

    def __init__(self, stdout: str, stderr: str, exit_code: int) -> None:
        super().__init__(
            f"command exited with code {exit_code}",
            code="COMMAND_EXITED",
            outcome="terminal",
        )
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code


def retain_failure_context(
    error: BaseException,
    environment_id: str,
    cleanup_error: BaseException | None = None,
) -> None:
    """Preserve authoritative identity and cleanup errors without replacing failure.

    Also works for cancellation exceptions, which must retain their original
    type. A known-created ID takes precedence over a transport lookup name.
    """
    context: Any = error
    context.environment_id = environment_id
    if cleanup_error is not None:
        previous = getattr(error, "cleanup_errors", ()) or ()
        context.cleanup_errors = (*previous, cleanup_error)
