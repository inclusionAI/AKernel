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

"""Exercise unrestricted, blocked, DNS-filtered, and allowlisted networking."""

import os
import shlex

from akernel_sdk import (
    BackendOperationError,
    NetworkPolicy,
    NetworkRule,
    PortRange,
    Sandbox,
)

RUNTIME = os.environ.get("AKERNEL_TEST_RUNTIME", "runsc")
BLOCKED_HOST = os.environ.get("AKERNEL_TEST_NETWORK_BLOCKED_HOST", "github.com")
ALLOWED_HOST = os.environ.get("AKERNEL_TEST_NETWORK_ALLOWED_HOST", "example.com")
WILDCARD_HOST = os.environ.get("AKERNEL_TEST_NETWORK_WILDCARD_HOST", "api.github.com")
NETWORK_PORT = int(os.environ.get("AKERNEL_TEST_NETWORK_PORT", "443"))


def tcp_connection(host: str, port: int) -> str:
    program = f"exec 3<>/dev/tcp/{shlex.quote(host)}/{port}"
    return f"bash -c {shlex.quote(program)}"


def direct_connection() -> str:
    return tcp_connection("1.1.1.1", 53)


def assert_connection_denied(sandbox: Sandbox, command: str, timeout: int) -> None:
    try:
        result = sandbox.commands.run(command, timeout=timeout)
    except BackendOperationError as error:
        cause = error.__cause__
        processing_deadline = (
            getattr(cause, "status_code", None) == 504
            and getattr(cause, "code", None) == "PROCESSING_DEADLINE_EXCEEDED"
        )
        assert "timed out" in str(error).lower() or processing_deadline, error
    else:
        assert result.exit_code != 0


def main() -> None:
    with Sandbox(runtime=RUNTIME) as unrestricted:
        result = unrestricted.commands.run(
            tcp_connection(BLOCKED_HOST, NETWORK_PORT), timeout=30
        )
        assert result.exit_code == 0, result.stderr
        print("Unrestricted DNS and connection succeeded.")

    with Sandbox(runtime=RUNTIME, network_policy=NetworkPolicy.block()) as blocked:
        control = blocked.commands.run("printf 'control plane works'")
        assert control.exit_code == 0, control.stderr

        assert_connection_denied(blocked, direct_connection(), timeout=10)
        print("Block policy denied an external connection.")

        blocked.files.write("/tmp/acl.txt", "RuntimeRPC fallback")
        assert blocked.files.read("/tmp/acl.txt") == "RuntimeRPC fallback"
        print("Commands and filesystem operations remain available.")

    dns_policy = NetworkPolicy.deny_dns(BLOCKED_HOST, "*." + BLOCKED_HOST)
    with Sandbox(runtime=RUNTIME, network_policy=dns_policy) as dns_filtered:
        assert_connection_denied(
            dns_filtered, tcp_connection(BLOCKED_HOST, NETWORK_PORT), timeout=30
        )

        allowed = dns_filtered.commands.run(
            tcp_connection(ALLOWED_HOST, NETWORK_PORT), timeout=30
        )
        assert allowed.exit_code == 0, allowed.stderr
        print("Allowed DNS and connection succeeded.")

    allowlist = NetworkPolicy.allowlist(
        [
            NetworkRule(
                domain="*." + BLOCKED_HOST,
                protocol="tcp",
                port_range=PortRange(NETWORK_PORT),
                priority=200,
            ),
            NetworkRule(
                cidr="192.0.2.10",
                protocol="tcp",
                port_range=PortRange(8443),
            ),
        ]
    )
    with Sandbox(runtime=RUNTIME, network_policy=allowlist) as restricted:
        allowed = restricted.commands.run(
            tcp_connection(WILDCARD_HOST, NETWORK_PORT), timeout=30
        )
        assert allowed.exit_code == 0, allowed.stderr

        assert_connection_denied(
            restricted, tcp_connection(ALLOWED_HOST, NETWORK_PORT), timeout=10
        )
        print("Generic egress allowlist enforced domain and port rules.")
    with Sandbox(runtime=RUNTIME) as dynamic:
        dynamic.update_network_policy(NetworkPolicy.block())
        assert_connection_denied(dynamic, direct_connection(), timeout=10)

        dynamic.update_network_policy(
            NetworkPolicy.deny_dns(BLOCKED_HOST, "*." + BLOCKED_HOST)
        )
        allowed = dynamic.commands.run(
            tcp_connection(ALLOWED_HOST, NETWORK_PORT), timeout=30
        )
        assert allowed.exit_code == 0, allowed.stderr

        dynamic.update_network_policy(None)
        cleared = dynamic.commands.run(
            tcp_connection(BLOCKED_HOST, NETWORK_PORT), timeout=30
        )
        assert cleared.exit_code == 0, cleared.stderr
        print("Dynamic policy replacement and clearing succeeded.")


if __name__ == "__main__":
    main()
