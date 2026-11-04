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

"""Pause/reconnect/list by ID. Requires a checkpoint-capable AKernel deployment.

Run: python sdk/python/examples/lifecycle.py --image python:3.13-slim
Only the sandbox created by this example is deleted.
"""

import argparse

from akernel_sdk import Sandbox, SandboxQuery, SandboxState


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    source = Sandbox.create(image=args.image, idle_timeout=600)
    identity = source.sandbox_id
    attached = None
    try:
        source.files.write("/tmp/lifecycle-example", "saved")
        assert source.beta_pause()
        assert not source.pause()
        source.close()
        assert Sandbox.get_info(identity).state == "paused"
        pages = Sandbox.list(query=SandboxQuery(state=[SandboxState.PAUSED]), limit=20)
        found = False
        while pages.has_next:
            found |= any(info.sandbox_id == identity for info in pages.next_items())
        assert found
        attached = Sandbox.connect(identity)
        assert attached.sandbox_id == identity
        assert attached.files.read("/tmp/lifecycle-example") == "saved"
        print("Restored the same sandbox:", identity)
    finally:
        if attached is not None:
            attached.close()
        source.close()
        Sandbox.kill(identity)


if __name__ == "__main__":
    main()
