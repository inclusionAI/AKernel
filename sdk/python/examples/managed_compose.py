"""Use an operator-authorized runtime profile; no client Docker configuration."""

import argparse

from akernel_sdk import ComposeProject, DockerRuntime, Sandbox


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("context")
    parser.add_argument("--profile", required=True)
    args = parser.parse_args()
    project = ComposeProject.prepare(
        args.context,
        [args.context + "/docker-compose.yaml"],
        runtime=DockerRuntime(profile=args.profile),
        network="bridge",
    )
    sandbox = Sandbox(**project.sandbox_options())
    try:
        project.start_runtime(sandbox)
        project.build()
        project.pull()
        project.up()
        print(project.ps())
    finally:
        sandbox.kill()


if __name__ == "__main__":
    main()
