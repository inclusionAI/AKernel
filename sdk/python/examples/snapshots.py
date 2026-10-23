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

"""Reusable snapshot lifecycle. Requires AKERNEL_* settings and runtime support.

Run: python sdk/python/examples/snapshots.py --image python:3.12-slim
The example creates and removes only its own sandboxes and reusable snapshot.
"""

import argparse

from akernel_sdk import Sandbox


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    snapshot = None
    try:
        with Sandbox(image=args.image) as source:
            source.files.write("/tmp/snapshot-example", "saved")
            snapshot = source.create_snapshot(name="sdk-example")
            source.files.write("/tmp/snapshot-example", "changed")
            assert Sandbox.get_snapshot(snapshot).snapshot_id == snapshot.snapshot_id
            pages = Sandbox.list_snapshots(name="sdk-example", limit=10)
            found = False
            while pages.has_next:
                found |= any(
                    s.snapshot_id == snapshot.snapshot_id for s in pages.next_items()
                )
            assert found
        with Sandbox.from_snapshot(snapshot) as restored:
            assert restored.id != source.id
            assert restored.files.read("/tmp/snapshot-example") == "saved"
            print("Restored a new sandbox from", snapshot.snapshot_id)
    finally:
        if snapshot is not None:
            Sandbox.delete_snapshot(snapshot)


if __name__ == "__main__":
    main()
