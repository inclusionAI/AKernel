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

"""Exercise unrestricted, fully blocked, and DNS-denylisted networking."""

import shlex

from akernel_sdk import NetworkPolicy, Sandbox


def tcp_connection(host: str, port: int) -> str:
    program = f"exec 3<>/dev/tcp/{shlex.quote(host)}/{port}"
    return f"bash -c {shlex.quote(program)}"


def direct_connection() -> str:
    return tcp_connection("1.1.1.1", 53)


def main() -> None:
    with Sandbox() as unrestricted:
        result = unrestricted.commands.run(
            tcp_connection("github.com", 443), timeout=30
        )
        assert result.exit_code == 0, result.stderr
        print("Unrestricted DNS and connection succeeded.")

    with Sandbox(network_policy=NetworkPolicy.block()) as blocked:
        control = blocked.commands.run("printf 'control plane works'")
        assert control.exit_code == 0, control.stderr

        external = blocked.commands.run(direct_connection(), timeout=10)
        assert external.exit_code != 0
        print("Block policy denied an external connection.")

        blocked.files.write("/tmp/acl.txt", "RuntimeRPC fallback")
        assert blocked.files.read("/tmp/acl.txt") == "RuntimeRPC fallback"
        print("Commands and filesystem operations remain available.")

    dns_policy = NetworkPolicy.deny_dns("github.com", "*.github.com")
    with Sandbox(network_policy=dns_policy) as dns_filtered:
        denied = dns_filtered.commands.run(
            tcp_connection("github.com", 443), timeout=30
        )
        assert denied.exit_code != 0

        allowed = dns_filtered.commands.run(
            tcp_connection("example.com", 443), timeout=30
        )
        assert allowed.exit_code == 0, allowed.stderr
        print("Allowed DNS and connection succeeded.")


if __name__ == "__main__":
    main()
