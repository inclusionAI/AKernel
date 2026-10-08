# ADX workload checkpoint validation

> Historical evidence for the 2026-09-22 validation image. Component names and
> the release-lock references below describe that run. The current build uses
> Coordinator, Adxlet, Ingress and EXECD and installs its pinned OBS release
> directly from the Dockerfiles.

Verified on 2026-09-22 on the dedicated Linux x86_64 standalone host with gVisor.
The workload interface remains `POST /checkpoint` on `/run/akernel/rrt.sock`.
RRT publishes a pending request to its owning Node Manager, which calls sandboxd
with `leave_running=true` and registers a local recovery point before replying.
The source stays running; SDK `reload()` restores the same logical sandbox.

The ADX implementation is commit `36a8258` (`fix/rrt-checkpoint-socket`). The
validation image `akernel-adx-validation:rrt-checkpoint` has ID
`sha256:6a9dee3bf0e3c7e74f655811ea8ebda0f2eadfab216ea1ae9c064264ad171434`.
It overlays the updated Node Manager, RRT and socket configuration on the #71
validation image. AKernel's existing rootfs was repacked with RRT; sandboxd was
unchanged. The repository release lock still selects #71, which does not include
this bridge: publish the ADX fix and update the artifact lock before relying on
workload checkpoints in a normal packaged deployment.

Results:

- `make sdk-check`: 253 tests passed, Ruff and mypy passed.
- `make deploy-script-check`: passed.
- `test_sandbox.py` standalone integration: 6 passed, 1 skipped (custom OCI image
  not configured), zero failures/errors.
- `test_internal_checkpoint_reload_and_reverse_tunnel` proved Unix socket
  completion, source continuation, reload to checkpoint-era file contents and
  reverse-tunnel recovery. The SDK retains its existing command/file/PTY facades.
- A route propagation race was reproduced after reload. The adapter now uses a
  read-only process listing before returning success; it does not repeat reload.
  Temporary 409 conflicts are bounded, and terminal errors are not retried.
- The task container was removed; persisted test logs and checkpoint data remain
  in the isolated validation worktree for diagnosis.

Remote log: `/var/log/akernel-rrt-checkpoint-e2e-final-20260922.log`.
Custom OCI and Kubernetes were not validated by this run.

## Firecracker follow-up (2026-09-22)

The same integration suite was rerun with `AKERNEL_TEST_RUNTIME=firecracker`
on the x86_64 host (Linux `6.8.0-137-generic`, accessible `/dev/kvm`, KVM API 12).
It passed: **6 cases passed, 1 OCI/Nydus case skipped**, zero failures/errors,
73.973 seconds. The checkpoint/reload/file rollback/reverse-tunnel case passed.
Command/process listing, filesystem and all three PTY cases also passed.
The test container was stopped and removed; logs and task data were retained.

The gVisor validation image did not contain the optional Firecracker payload.
The FC image adds the pinned `v1.16.1-akernel.3` VMM, guest kernel `6.1.177`,
and virtiofsd `1.14.0` from the existing AKernel image after manifest/hash
verification. The guest initrd was built from the same sandboxd source
`7d2af7f52eeaae5203fc9f4fe3dadffc357eac3c`; the sandboxd daemon was unchanged.

- Image: `akernel-adx-validation:rrt-checkpoint-fc`.
- Image ID: `sha256:e5e653a06555cac24d490550b8aad770df3b1d7ba1984a26b7da818f5b9243d9`.
- VMM SHA256: `41133331123c05d635a1a4a61a1eb41f078e9a216b5a197c1398e4e64e957cbf`.
- Kernel SHA256: `78c482cb6904f12c7de16e434de3e8163e9bb7d8abac982a508250ca59abe9af`.
- Initrd SHA256: `1753c338a6a100105ea0e93ce1b94031ff6b9820f7f62292a1c662c82ed6b912`.
- Remote log: `/var/log/akernel-adx-fc-e2e-20260922.log`.
- Local log: `out/remote-validation/logs/firecracker-e2e.log`.

This remains an overlay validation image. The formal ADX release and AKernel
artifact lock update are still pending; this result does not cover custom OCI,
cross-node cloning or Kubernetes deployment.

## Review compatibility regression (2026-09-22)

The revised SDK and simplified standalone profile were validated using only
`AKERNEL_SERVER_ADDRESS=127.0.0.1` and `AKERNEL_TOKEN`. No backend selector,
URL scheme, or gateway override was supplied. The existing `data/token` path
remains the credential entry point, and generated certificates are reused.

- SDK: 227 unit tests, Ruff, and mypy passed. Tests of the removed adapter were
  retired; address parsing, legacy selector aliases, explicit cleanup, and the
  GC-under-HTTPX-lock regression remain covered.
- Deployment: 22 runtime/certificate/Secret/Helm/Terraform contract tests passed;
  shell syntax and Terraform input-reference checks passed.
- gVisor: 6 passed, 1 custom OCI case skipped (52.085 seconds).
- Firecracker: 6 passed, 1 custom OCI case skipped (71.901 seconds).
- Remote run log: `/var/log/akernel-review-78-final-e2e.log`.
- Local run log: `out/pr/standalone-final-e2e.log`.

The runtime image is still derived from the checkpoint validation overlay above,
with the revised configuration and entrypoint scripts. This run verifies the
SDK and deployment behavior; it does not verify a clean all-in-one build from
the currently pinned release. Public artifact publication, the formal package
update, custom OCI validation, and live Kubernetes validation remain pending.

## Internal network mode regression (2026-09-22)

Standalone now selects `internal_security: network`. Internal RPC and Edge-to-node
forwarding do not load component certificates. Public HTTPS and API key checks
remain enabled; the SDK still uses the existing server address and token.
Component role declarations are trusted within the deployment network, while
node-session, ownership and tenant checks remain in place.

The test started from a new data directory and asserted that `data/adx/tls`
contained only `edge-public.pem` and `edge-public.key`:

- gVisor: 6 passed, 1 custom OCI case skipped (51.158 seconds).
- Firecracker: 6 passed, 1 custom OCI case skipped (72.260 seconds).
- Both included command, filesystem, PTY and workload checkpoint/reload with
  reverse-tunnel recovery. The task container was removed after testing.
- Certificate/Secret generation and Helm contracts: 13 tests passed; deployment
  shell checks passed. Existing public certificates are reused on restart.
- Image: `akernel-adx-validation:network-mode`.
- Image ID: `sha256:1e6f257b828b17a735c7d8e12b59e73471ba6d326fe2b06387942c397b145e44`.
- Remote log: `/var/log/akernel-network-mode-e2e.log`.
- Local log: `out/pr/network-e2e.log`.

This image overlays the updated ADX control and gateway binaries on the reviewed
checkpoint image. The formal #71 artifact pin still needs replacement with a
release containing both changes. Custom OCI and live Kubernetes were not tested.

## Deployment-owned keys and administrator rotation (2026-09-22)

`start.sh` now creates the key before starting the container. The key is written
atomically with mode 0600 and reused; `data/token` points to the current file.
The independent certificate helper was removed. Public TLS initialization is
part of the existing service script and does not create API keys.

The validation started with a fresh data directory using the updated Master:

- gVisor: 6 passed, 1 OCI case skipped (51.381 seconds).
- Firecracker: 6 passed, 1 OCI case skipped (73.229 seconds).
- After atomic key-file replacement and service restart, `data/token` read the
  new key; the public resources API accepted it with HTTP 200 and rejected the
  old key with HTTP 401. A second restart preserved both results.
- Deployment checks: 22 tests passed; shell syntax checks passed. The mocked
  Kubernetes retrieval test changed the Secret and verified `make token`'s
  helper read the replacement and refreshed its protected output file.
- ADX real Redis storage: 26 passed; RPC: 20 passed, including Master restart
  with the unchanged key followed by replacement and old-key rejection.
- Image: `akernel-adx-validation:admin-key`.
- Image ID: `sha256:5f151ac9ed048174972e3025ecd75dc8e6e20e54b564d983079aaf8a2b4522ae`.
- Remote log: `/var/log/akernel-admin-key-e2e.log`.
- Local log: `out/pr/admin-key/e2e.log`.

The image overlays task-built ADX binaries and AKernel startup scripts. It is
not the formal #71 package. Its replacement remains necessary before normal
packaged deployment. This round did not exercise live Kubernetes, custom OCI,
Redis crash recovery or cache expiry with an uninterrupted ingress process.


## Kubernetes follow-up (2026-09-22)

The ADX overlay was published as a single-platform linux/amd64 registry manifest
and deployed to the four-worker Kubernetes environment. The accepted digest is
`sha256:d4ac9c13759c2b637aaa18b1c7123709abd458a292b4de14b150f83fcc4f81b5`.
It includes AKernel `d245088` with the systemd environment handoff fix and ADX
`9ba34e6`. The formal ADX package lock is still unchanged; this does not prove a
clean build from that lock. Traefik was disabled and SDK traffic used the direct
Edge HTTPS control / HTTP data listeners through internal Service port forwarding.

- All six control, node and Redis containers matched the published digest.
- Chart contract: 15 tests passed. Systemd contract: 7 runtime and 2 service tests
  passed; deployment script checks and `git diff --check` passed.
- The final runsc SDK suite passed 6 cases, with 1 custom OCI case skipped,
  in 81.151 seconds. Command, filesystem, PTY, workload checkpoint, reload and
  reverse tunnel were covered. Missing and invalid API keys returned HTTP 401.
- All four nodes advertised allocatable resources.
- Redis rejected unauthenticated commands. Its independent password was retained
  across a Helm upgrade. The CNI did not enforce the additional NetworkPolicy;
  that policy is not accepted as the sole access boundary on this environment.
- A Control container restart within the same Pod recovered Master and Edge.
  Generated supervisor state no longer persists across container restarts, which
  avoids collisions with PID-named rendered configuration directories.
- The other existing namespace's Deployment and DaemonSet specifications were
  unchanged. The old etcd PVC was retained.

Evidence: `out/cn-north-4-upgrade/REPORT.md`, `control-restart.log`,
`redis-auth-rejection.log`, and `sdk-e2e-final.log` in that directory.
This run does not cover cluster Firecracker, custom OCI or public load-balancer
access. Platform-owned sandbox egress denies for cluster CIDRs remain unimplemented;
user-configured network policy and Redis authentication do not establish that
sandbox isolation contract.
