# Real native and E2B SDK qualification

Run against an operator-owned deployment with both protocols on the same image, RuntimeProfile and state/storage services. Install the published `akernel-sdk==0.2.0rc5` and `e2b==2.52.0` in a separate virtual environment; do not edit either SDK. Enable E2B and the persistent log initialization as described in [the deployment guide](../../docs/dual-sdk-and-application-logs.md). Prepare an E2B template with the official `Template.build(Template().from_image(IMAGE), name=...)` API first. Template build support is limited to the backend's implemented image-based path.

Keep a protected JSON connection file containing `api_url`, `gateway_address`, `api_key` and `verify_tls: true`. Both URLs must use the existing trusted HTTPS entry and a certificate-matching address. Supply its trusted CA; do not disable verification. E2B's Rust HTTP resolver may not honor a Python socket override: use real DNS, certificate SAN IPs, or a dedicated test Pod's `hostAliases`.

```bash
python qualification.py native --connection-file "$CONNECTION" --ca "$CA" --result "$NEW_RESULT"
python qualification.py e2b --connection-file "$CONNECTION" --ca "$CA" --other-key-file "$FOREIGN_TENANT_KEY" --template base --result "$ANOTHER_NEW_RESULT"
python qualification.py native-logs --connection-file "$CONNECTION" --ca "$CA" --image "$BUSINESS_IMAGE_DIGEST" --source-node "$NODE" --result "$NATIVE_LOG_RESULT"
python qualification.py e2b-logs --connection-file "$CONNECTION" --ca "$CA" --template "$BUSINESS_TEMPLATE" --result "$E2B_LOG_RESULT"
```

Each phase owns its sandbox IDs and snapshots and attempts all cleanup in `finally`. Results are private, cannot overwrite an earlier run, and are marked passed only after cleanup succeeds. Log phases deliberately retain uniquely named log files for independent durability checks. Inspect these same files on the host and Node Pod after sandbox deletion and a drained Node Pod replacement. Repeat initialization on a fresh verified data disk to qualify node provisioning; rendering a DaemonSet does not prove persistence or cloud-node initialization.

Core phases verify real process memory and files across leave-running snapshots, same-ID pause/connect and independent clones; they also exercise commands, PTY exit7 and a published HTTP endpoint. E2B verifies a missing envd token, an invalid API key and a separately minted foreign-tenant key. The port probe uses the supported fixed HTTPS entry and routing headers. It does not qualify wildcard `get_host()` DNS, signed public URLs, Code Interpreter or Desktop.

Use `--source-node` and `--target-node` for native placement. E2B's official create API has no node selector. With `--hold-seconds`, inspect actual sandbox ownership and physical runtime placement while the source and clone are alive; requested node names alone do not prove cross-node restore. Independently make the source worker unavailable, restore on each clean target and verify checkpoint/cache ownership and background restore protection. Cross-node envd isolation also needs real guest-to-guest TCP attempts; an HTTP401 only tests authentication.

The native log phase explicitly rebinds identical mounts when cloning snapshots and tests both RO and RW restoration. HostPath data remains external, node-local state; the probe does not claim cross-node copying of logs or open external files. The E2B log phase verifies the deployment-owned fixed `/app/logs` mount and non-root default user. It does not invent an arbitrary E2B HostPath parameter or request-scoped registry credentials. Use the actual business private image/template, retain the existing node registry secret, and record its independent startup and UID/GID results.
