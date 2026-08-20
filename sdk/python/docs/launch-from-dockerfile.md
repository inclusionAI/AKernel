# Launch a sandbox from a Dockerfile

Dockerfile direct launch is a supported AKernel SDK capability and will remain
available. Its documented strict subset evolves incrementally with production
experience; unsupported inputs continue to fail closed. The specific API surface
may evolve, with documentation and migration guidance for material changes. It
is not a general-purpose Docker build facility and does not replace Docker,
BuildKit, a registry, or an external image build pipeline.

## Scope and root filesystem semantics

Direct launch applies a supported Dockerfile to a fresh AKernel sandbox through
the public `Sandbox`, `Commands`, and `Filesystem` facades. It needs no
BuildKit, Docker daemon, or registry push. `FROM` is **rootfs-only**: its image
supplies only the sandbox root filesystem. OCI `ENV`, `USER`, `WORKDIR`,
`CMD`, and `ENTRYPOINT` configuration is not inherited, so declare every
required runtime setting in the Dockerfile passed through `DockerContext`.

## Quick start

Create one context, inspect its diagnostic result, and launch only when it is
direct-launchable:

```python
from akernel_sdk import (
    DockerfileLaunch, LocalDockerContext, Sandbox, check_direct_launch,
)

context = LocalDockerContext("Dockerfile", context_dir=".")
check = check_direct_launch(context)
for warning in check.warnings:
    print(f"Dockerfile warning: {warning}")
if not check.direct_launchable:
    raise RuntimeError(check.reasons)

with Sandbox(dockerfile=DockerfileLaunch(context, run_timeout=300)) as sandbox:
    startup = sandbox.startup_command
    if startup is not None:
        result = startup.wait(timeout=60)
        print(result.exit_code, result.stderr)
    # Perform the application's own health check when it is long-lived.
```

See the maintained end-to-end
[`examples/dockerfile_launch.py`](../examples/dockerfile_launch.py).

## Precheck and DockerfileLaunch configuration

`check_direct_launch(context)` is diagnostic and returns reason codes including
`multi_stage`, `remote_add`, `no_from`, `unsupported_instruction`, and
`unsupported_syntax`. A false `direct_launchable` result means direct launch
will fail closed; build externally instead. `Sandbox(dockerfile=...)` parses
strictly, `parse_dockerfile(strict=False)` is diagnostic only, and
`apply_dockerfile()` also rejects parsed unsupported syntax.

Configure the immutable `DockerfileLaunch` value:

| Field | Meaning |
| --- | --- |
| `context: DockerContext` | Dockerfile text and build context to apply. |
| `auto_start_cmd: bool = True` | Dispatch the resolved `CMD`/`ENTRYPOINT` in the background after build-time instructions. |
| `run_timeout: int = 600` | Positive timeout in seconds for each `RUN` instruction. |

`dockerfile`, `image`, and `rootfs` are mutually exclusive constructor sources.

## Supported Dockerfile subset

| Supported direct-launch subset | Rejected; use an external build |
| --- | --- |
| Exactly one literal `FROM`, optionally `AS alias`; shell-form `RUN` | Multiple stages, `COPY --from`, `FROM` flags or variables, and exec-form `RUN` |
| Shell-form local `COPY` and `ADD` paths: files, directories, `.`, and wildcards; literal `--chown`; literal local tar extraction for `ADD` | JSON-form `COPY`/`ADD`, remote `ADD` URLs, `--chmod`, `--link`, unknown flags, or build-time variable expansion |
| Literal `ENV`, absolute `WORKDIR`, named `USER` values such as `app` or `root`, and `EXPOSE` metadata | Any `ARG`, relative `WORKDIR`, `USER user:group` or numeric UID/GID values, and `VOLUME`, `LABEL`, `HEALTHCHECK`, `SHELL`, `STOPSIGNAL`, `ONBUILD`, `MAINTAINER`, or unknown instructions |
| Exec- or shell-form `CMD` and `ENTRYPOINT`, normalized and combined | — |

## DockerContext extension contract

A `DockerContext` exposes Dockerfile text and structured
`DockerContextEntry` values from `walk()`. Each entry has a relative POSIX
`path`, `kind` of `file` or `directory`, and permission-bit `mode` from
`0o000` through `0o777`. Custom contexts must expose readable files, every
ancestor, and empty directories. Directory entries make empty directories and
their modes representable.

Custom contexts can implement `dockerfile_ignore()`. A
`(diagnostic_name, bytes)` tuple supplies the active matcher. Only `None`
falls back to the manifest-root `.dockerignore`; empty `bytes` still denote a
present higher-priority matcher. Dockerfiles and ignore files that belong to the
filesystem context must be enumerated by `walk()`. They remain ordinary context
entries that `COPY`/`ADD` can select unless the active matcher excludes them.

`LocalDockerContext` reads a local directory and rejects symbolic links. A
path-form Dockerfile uses adjacent `<Dockerfile>.dockerignore` when it exists,
including when empty; only its absence permits root `.dockerignore` fallback.
An inline Dockerfile creates no virtual context file. A Dockerfile outside the
context and its companion remain outside the manifest even if that companion
supplies the active matcher.

## Ignore filtering and COPY/ADD selection

Before sandbox operations, direct launch validates the manifest, then applies
Moby-compatible ordered ignore matching to files and directories.

- Ignore patterns are cleaned like `filepath.Clean` and comments require `#`
  in column one. Embedded `**` in `.dockerignore` can span directories; later
  `!` patterns can re-include descendants.
- A re-included descendant keeps required ignored directory ancestors as virtual
  selectable source directories. A fully ignored directory remains unavailable.
  Alphanumeric backslash escapes and nested POSIX character classes that Moby
  routes through its regular-expression engine are rejected, not literal.
- Direct launch plans every `COPY`/`ADD` before materializing files. Selected
  directory entries create empty and nested directories, and every copied child
  file or directory restores its own mode non-recursively.
- A literal directory source is a content container: its root mode is not
  inherited, though the destination is created as needed. A wildcard that
  matches a directory likewise copies its contents, not the matched name.

Dockerfile `COPY`/`ADD` source patterns use Go `filepath.Match`-style
**segment** matching. Unlike `.dockerignore`, source `**` has the same
one-segment behavior as `*`. `[^a]` negates a character class, while `[!a]`
matches either `!` or `a`. Backslash escapes are outside this strict source
subset and malformed classes fail closed. When one wildcard expands to multiple
top-level sources after `.dockerignore` filtering, the destination must end in
`/`. Unsafe paths and destination collisions fail closed.

`--chown` affects only files and directories created by the current instruction.
A local tar `ADD` accepts only regular files and directories with safe paths,
preserves tar member metadata, and always extracts as the builder/root identity
before applying `--chown`. Remote `ADD` URLs are rejected; the SDK never
fetches them.

## Fail-closed security boundary

The manifest is validated before selectable files are opened. Secure local
opening requires platform support for directory-relative file descriptors and
no-follow flags; unsupported platforms fail closed rather than weakening the
local-context boundary. Unsupported Dockerfile syntax, malformed patterns,
unsafe paths, missing sources, and target collisions also fail closed.

## Startup and lifecycle

Each launch executes `RUN`, `COPY`, and `ADD` again in a new sandbox. There
is no snapshot or build cache. After build-time instructions finish, the SDK
polls **sandbox readiness** and, when enabled, dispatches the resolved
`CMD`/`ENTRYPOINT` in the background. `Sandbox.startup_command` is a
`CommandHandle | None`; use `wait()` for finite commands or `kill()` when
needed. Construction confirms dispatch, not that the application remains
running or passes a health check. Callers own application readiness checks.

## External-build fallback

For Dockerfiles outside this subset, or build-once reuse, build with the chosen
external build system and use `Sandbox(image=...)`. Then explicitly launch the
desired command with `sandbox.commands.run(..., background=True)`; image
configuration does not auto-start `CMD` or `ENTRYPOINT` in this SDK path.

## Parser, matching, and license references

Line-level parsing uses
[`dockerfile-parse`](https://github.com/containerbuildsystem/dockerfile-parse)
(BSD-3-Clause). `.dockerignore` behavior follows
[`moby/patternmatcher`](https://github.com/moby/patternmatcher) (Apache-2.0).
The value post-processing approach was informed by the
[`E2B Python SDK`](https://github.com/e2b-dev/E2B/tree/main/packages/python-sdk) ([MIT license](https://github.com/e2b-dev/E2B/blob/main/packages/python-sdk/LICENSE)).
