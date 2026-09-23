# SDK end-to-end test levels

The directories below encode deployment requirements so that a narrow gate
never discovers tests that need a larger environment by accident.

- `standalone/`: public SDK contracts that must pass on one AKernel node. Full
  deployments rerun this suite against their public endpoints.
- `multi_vm/`: cross-node placement, routing, failover, and node-loss cases.
- `full/`: Kubernetes deployment, multi-worker, storage, accelerator, and
  infrastructure fault cases.

Keep unit tests under `tests/unit/`. The existing `tests/integration/` suite
contains runtime-heavy checkpoint and reverse-tunnel coverage and is selected
explicitly by the standalone gate. New cases must be added to the smallest
deployment level that provides all required capabilities.
