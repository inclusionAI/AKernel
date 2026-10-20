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

"""Bind a prepared node path using trusted management credentials.

The node runtime must already see the source and allow the sandbox process to
access it. Default mode only lists the read-only mount. --write explicitly
creates one unique log file and leaves it on the node after sandbox deletion.
Use operator-controlled source paths; SDK validation does not authorize access
to the node's filesystem.
"""

import argparse
import posixpath
import shlex
import uuid

from akernel_sdk import Mount, Sandbox


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--host-path", required=True)
    parser.add_argument("--target", default="/apps/logs")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    mount = Mount(
        target=args.target, host_path=args.host_path, read_only=not args.write
    )
    with Sandbox(node_id=args.node_id, mounts=[mount], cpu=1000, memory=1024) as sb:
        command = "ls -ld -- " + shlex.quote(args.target)
        if args.write:
            marker = "akernel-sdk-" + uuid.uuid4().hex
            filename = marker + ".log"
            target = shlex.quote(posixpath.join(args.target, filename))
            command = f"printf '%s\\n' {shlex.quote(marker)} > {target} && cat {target}"
            print("Log on the selected node:", posixpath.join(args.host_path, filename))
        result = sb.commands.run(command)
        if result.exit_code != 0:
            raise RuntimeError(result.stderr or result.stdout)
        print(result.stdout)


if __name__ == "__main__":
    main()
