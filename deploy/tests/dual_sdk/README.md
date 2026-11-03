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


## Source outage and envd isolation

`e2b_checkpoint.py` captures and restores a real 19 MiB process-memory workload,
ordinary files and a 64 MiB sparse file through the official SDK. Run `capture`
on the sole admitted source, verify the recorded physical placement, then drain
and stop the source host. With the source confirmed stopped, admit one clean
peer at a time and run `restore --expected-node ...` against the saved state.
Confirm the artifact ID was absent from each target cache before the outage.
The operator owns host stop/start and must restore the source even on failure.
Keep each state/result file and failed attempt rather than overwriting it.

```bash
python e2b_checkpoint.py capture --connection-file "$CONNECTION" --ca "$CA" --state "$NEW_STATE" --expected-node "$SOURCE"
# After an independently recorded source-host outage and target admission:
python e2b_checkpoint.py restore --connection-file "$CONNECTION" --ca "$CA" --state "$NEW_STATE" --expected-node "$PEER" --result "$COLD_RESULT"
python envd_isolation.py --connection-file "$CONNECTION" --ca "$CA" --source-node "$SOURCE" --peer-node "$PEER" --internal-entry "$EXISTING_INTERNAL_TLS_ENTRY" --manage-peer-scheduling --result "$ISOLATION_RESULT"
```

The isolation phase is maintenance-only: exactly the source must initially admit
new allocations. It temporarily admits the peer through the existing admin API,
then blocks it in cleanup. It creates an authenticated E2B victim, proves SDK
file access, denies a same-node guest's TCP access to the victim's envd address,
and checks the peer cannot reach a Node-Pod envd listener or bypass envd token
authentication through the shared Ingress. No control or envd credential enters
an attacker guest. Restore the desired normal admission state after qualification.
This does not qualify Kubernetes NetworkPolicy or arbitrary cross-node guest-IP
routing; the overlay subnet may overlap on different nodes.

E2B background commands default to a 60-second lifetime in the qualified SDK.
Use the public `commands.run(..., background=True, timeout=0)` option for a
checkpoint workload that must remain alive past that deadline. A background
command's deadline is independent of the sandbox lifetime; do not restart a
process after restoration and count that as restored memory. The phased probe
uses the unlimited command timeout and the operator should delay restoration
past the default deadline to verify the intended contract.

For the default TLS transport contract, optionally add `--http-port-entry` to
each core phase. It asserts native public application ports work over HTTP
while E2B returns426 with the HTTP opt-in disabled. Keep that negative contract
separate from an explicitly HTTP-enabled deployment.

For `adx.e2b.allowHttp=true` on a supporting ADX build, run the official SDK
without CA environment overrides using `http_qualification.py`:

```bash
# Protected JSON: {"origin": "http://EXISTING_IP", "api_key": "..."}
env -u SSL_CERT_FILE -u SSL_CERT_DIR -u REQUESTS_CA_BUNDLE -u CURL_CA_BUNDLE \
  python http_qualification.py --connection-file "$HTTP_CONNECTION" \
  --other-key-file "$FOREIGN_TENANT_KEY" --template base --result "$NEW_RESULT"
```

The probe checks create/commands/files/PTY/application port/delete, same-ID
pause/connect, leave-running Snapshot, two independent restored clones, invalid
API keys/foreign tenants/missing and cross-sandbox envd tokens, forbidden envd
init/Execd, and the retained native/admin TLS boundary. Cleanup failure fails
the run. It uses the existing fixed HTTP entry and routing headers; `get_host()`
wildcard DNS and public signed URLs remain separate capabilities. The old
`qualification.py` TLS phase and actual private business image/log qualification
remain necessary; HTTP success does not imply HostPath or registry support.
For cross-node E2B restores, `e2b_checkpoint.py --e2b-origin http://EXISTING_IP`
uses HTTP for the official E2B SDK and trusted native TLS separately for physical
placement inspection. Record source availability and actual target/cache state.
