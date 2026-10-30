# Managed Docker image bridge

This helper exposes sandboxd-provided immutable ROFS image mounts as committed containerd snapshots, with private upper/work directories owned by the sandbox. It does not introduce another registry layer cache. Metadata-only image registration and source Dockerfile/Compose discovery use containerd, compose-go and BuildKit parsers.

The snapshotter originated in tianyuzhou95's agent-worker POC, commit `9b3434d36086d920b6bb7f3322da642381a254b8`. The managed-upper runtime contract was ported to sandboxd separately. Preserve this provenance when moving or publishing the helper.

Build the host/guest helper on a Linux/amd64 client:

```sh
cd builder/image-bridge
CGO_ENABLED=0 go test ./...
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -o /path/to/akernel-image-bridge .
```

The Python SDK's `prepare_compose_images()` invokes `--mode discover`, resolves tags and verifies OCI manifest/config bytes. Credentials stay on the client; the runtime image manager must independently be authorized to fetch the pinned images. Only metadata is uploaded. Layer bytes are mounted by the existing sandboxd OCI/ROFS provider, which owns node-wide caching and distribution.

`DockerRuntime.sandbox_options()` supplies the ROFS mounts and `fileBackedTmpfsMounts` to fresh creation and snapshot restore. `DockerRuntime.start()` uploads the helper when configured, starts its private containerd and bridge, registers original image aliases, then starts dockerd. Call start only once on a fresh sandbox; never on a restored sandbox. Failed startup is cleaned up by the owner deleting the newly created sandbox, not by touching shared node state.

Automatic Compose uses these mounted roots for image-only services as well as build bases. After registration, the SDK verifies aliases against their pinned target digests and checks the committed ROFS snapshots. It does not use native Compose pull for those inputs: Docker Compose can pull digest references even with `--policy missing`, which would incorrectly enter the ordinary layer extraction path. Automatic `up` also disables repeat pulls. An absent registration fails instead of bypassing the mount plan or silently copying image layers into private storage. Different image manifests/configurations can legitimately share one chain ID; the validated configuration and chain bind their ROFS roots before verification allows that sharing.

The current SDK requires **discovery schema 2**. Check with `akernel-image-bridge --mode capabilities`; older helpers (including the earlier `72e706e2` attachment) must be replaced before using this SDK. The probe sends no project data. Discovery rejects Dockerfile/build-context paths or `.env` links escaping the frozen project and does not inherit unspecified client environment variables. External `include` / `extends.file`, remote contexts and platform emulation are outside the source-preview profile; pass explicit supported Compose files instead. The runtime bundle and Sandboxd contract do not change with this client-helper update.

Use `DockerRuntime.default_image`, the public Docker Hub `docker.io/library/docker:28.3.3-dind` image. Fresh startup installs missing Alpine support packages, so no custom runtime image build or push is required. An explicitly supplied outer image must provide Docker 28.3.3, containerd/ctr, Python 3, findmnt/mount, Bash, iproute tools, iptables-legacy and iptables-legacy-restore. `builder/dind/Dockerfile` optionally packages these and the guest helper; use repository root as build context. The SDK can upload the matching helper from the client. No helper binary is downloaded automatically.

The default Linux/amd64 runsc profile preserves default/custom IPv4 Compose bridge networks, DNS and TCP/UDP userland port mappings. The operator must enable `extra_args = ["--net-raw=true", "--allow-packet-socket-write=true"]` under `[plugin.runtime.runsc]` for Docker 28. The SDK owns in-sandbox legacy forwarding/SNAT and per-service tunnel entries; Docker owns bridges/IPAM/DNS. After restore call `ComposeProject.reconnect_network()` to rebind the new outer TAP without restarting Docker. Task Dockerfiles and Compose files remain unchanged. Host networking is an explicit compatibility option. Remote contexts, runtime-created unknown image references and platform emulation remain outside this profile. Ordinary native Docker builds still run inside the sandbox. This is not a complete shared Docker content store: exporting/saving/pushing an image may still fetch layers.

The file-backed upper requires sandboxd managed tmpfs support and runsc checkpoint support for those mounts. It consumes the sandbox storage limit. Reflink must be configured by the deployment, on a compatible filesystem and same mount; the SDK does not change deployment durability policy or replace runtime bundles. Memory mode is an explicit alternative and counts file contents toward memory snapshots.
