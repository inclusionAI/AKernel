"""Run a Compose service process, snapshot it, and attach the original process.

Requires an owned endpoint with the managed Compose runtime prerequisites.
Example: python sdk/python/examples/compose_checkpoint.py ./project \
    --file ./project/compose.yaml \
    --bridge /absolute/path/akernel-image-bridge --service worker \
    --command 'exec python3 worker.py'
"""

import argparse
import tempfile
from pathlib import Path

from akernel_sdk import ComposeProject, DockerRuntime, Sandbox, ServiceProcessHandle


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--file", action="append", required=True, type=Path)
    parser.add_argument("--image", default=DockerRuntime.default_image)
    parser.add_argument("--bridge", required=True, type=Path)
    parser.add_argument("--service", required=True)
    parser.add_argument("--command", required=True)
    args = parser.parse_args()
    snapshot = None
    with tempfile.TemporaryDirectory(prefix="akernel-compose-example-") as output:
        project = ComposeProject.prepare(
            args.directory,
            args.file,
            runtime=DockerRuntime(bridge_binary=args.bridge, writable_mb=2048),
            auto_images=True,
            output_dir=Path(output),
        )
        try:
            with Sandbox(
                image=args.image,
                cpu=4000,
                memory=8192,
                storage_mb=30720,
                failover=False,
                **project.sandbox_options(),
            ) as source:
                project.start_runtime(source)
                project.build()
                project.pull()
                project.up()
                process = project.service(args.service).start_process(args.command)
                process.signal("stop")
                # An application also saves its aligned client-side state here.
                snapshot = source.create_snapshot(timeout=300)
                project_state, process_state = project.descriptor, process.descriptor
            # Context exit acknowledges deletion before the clone continues.
            with Sandbox.from_snapshot(
                snapshot, failover=False, **project.sandbox_options()
            ) as restored:
                attached = ComposeProject.attach(restored, project_state)
                attached.reconnect_network()
                original = ServiceProcessHandle.attach(
                    attached.service(args.service), process_state
                )
                original.signal("continue")
                print("Restored original service PID", original.pid)
                print("Exit code:", original.wait(timeout=300))
        finally:
            if snapshot is not None:
                Sandbox.delete_snapshot(snapshot)


if __name__ == "__main__":
    main()
