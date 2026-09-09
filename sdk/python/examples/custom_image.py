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

"""Launch an OCI image; use --runtime firecracker on a KVM-capable node."""

import argparse

from akernel_sdk import Sandbox

IMAGE = "ubuntu:24.04"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", default="runsc")
    parser.add_argument("--image", default=IMAGE)
    args = parser.parse_args()
    with Sandbox(
        image=args.image, runtime=args.runtime, cpu=1000, memory=2048
    ) as sandbox:
        result = sandbox.commands.run(". /etc/os-release && printf $PRETTY_NAME")
        assert result.exit_code == 0, result.stderr
        print(f"Sandbox {sandbox.id}: {result.stdout}")


if __name__ == "__main__":
    main()
