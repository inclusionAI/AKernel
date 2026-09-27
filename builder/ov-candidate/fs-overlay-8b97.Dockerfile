# Snapshot-protocol candidate 8b97a668: four-program tree overlay on the
# deployed canon-67e549c1-rel manifest. Identical assembly discipline to
# fs-overlay-67e549c1.Dockerfile: each program directory carries its own
# binary plus the COMPLETE formal-output library set, replacing the whole
# directory (delete-then-COPY, no stale-library merge). The base keeps the
# verified runsc, sandboxd, runtime rootfs, config and bootstrap-path fix.
# Materialized context is produced by assemble-fs-overlay-8b97.sh and pinned
# byte-for-byte by fs-overlay-8b97-inputs.sha (+ -types.txt for symlinks and
# modes).
FROM akernel-bm1/all-in-one@sha256:c503b392a11124285bacd8dc5663f7f64b5e6c6780562cf62ed0ad64d3a4b492
RUN rm -rf /opt/akernel-scheduler/function-proxy \
           /opt/akernel-scheduler/function-master \
           /opt/akernel-scheduler/runtime-manager \
           /opt/akernel-scheduler/function-agent
COPY fs-overlay-8b97/function-proxy /opt/akernel-scheduler/function-proxy
COPY fs-overlay-8b97/function-master /opt/akernel-scheduler/function-master
COPY fs-overlay-8b97/runtime-manager /opt/akernel-scheduler/runtime-manager
COPY fs-overlay-8b97/function-agent /opt/akernel-scheduler/function-agent
