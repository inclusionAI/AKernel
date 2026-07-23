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

"""Select a sandbox runtime.

The Kata case requires at least one cluster node that advertises Kata support.
Otherwise, the scheduler returns a no-resource error.
"""

from akernel_sdk import Sandbox


def run(runtime: str | None) -> None:
    sandbox = (
        Sandbox(cpu=1000, memory=2048)
        if runtime is None
        else Sandbox(runtime=runtime, cpu=1000, memory=2048)
    )
    with sandbox:
        result = sandbox.commands.run("uname -s")
        assert result.exit_code == 0, result.stderr
        print(f"{runtime or 'default'}: {sandbox.id} ({result.stdout})")


def main() -> None:
    run(None)
    run("runsc")
    run("kata")


if __name__ == "__main__":
    main()
