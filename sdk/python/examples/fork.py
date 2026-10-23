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

"""Fork one running sandbox; inspect partial results and release owned resources.

Run with AKERNEL_* settings: python sdk/python/examples/fork.py --image IMAGE
Requires reusable memory/filesystem snapshots on the deployed runtime.
"""

import argparse
import logging
import sys

from akernel_sdk import ForkCleanupError, Sandbox


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--count", type=int, default=2)
    args = parser.parse_args()
    results: list[Sandbox | Exception] = []
    pending_snapshot = None
    try:
        with Sandbox(image=args.image) as source:
            source.files.write("/tmp/fork-example", "captured")
            try:
                results = source.fork(count=args.count)
            except ForkCleanupError as error:
                results = [*error.results, *error.pending_sandboxes]
                pending_snapshot = error.snapshot_id
                raise
            for index, result in enumerate(results):
                if isinstance(result, Exception):
                    print(f"child {index} failed: {result}")
                    continue
                if result.files.read("/tmp/fork-example") != "captured":
                    raise RuntimeError("child did not inherit the captured file")
                result.files.write("/tmp/fork-example", f"child-{index}")
                print(f"child {index}: {result.id}")
            if source.files.read("/tmp/fork-example") != "captured":
                raise RuntimeError("a child write changed the source")
    finally:
        errors = []
        for result in results:
            if isinstance(result, Sandbox):
                try:
                    result.kill()
                except Exception as error:
                    errors.append(error)
        if pending_snapshot is not None:
            try:
                Sandbox.delete_snapshot(pending_snapshot)
            except Exception as error:
                errors.append(error)
        if errors:
            if sys.exc_info()[0] is None:
                raise RuntimeError("fork resource cleanup failed") from errors[0]
            for error in errors:
                logging.error("fork resource cleanup also failed: %s", error)


if __name__ == "__main__":
    main()
