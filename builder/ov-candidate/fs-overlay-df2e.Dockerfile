# Snapshot-protocol candidate df2e2f2a: four-program tree overlay on the
# deployed canon-8b97-rel manifest. Identical assembly discipline to the
# verified 67e549c1/8b97 recipes: each program directory carries its own
# binary plus the COMPLETE formal-output library set (files AND symlinks,
# modes preserved), replacing the whole directory (delete-then-COPY, no
# stale-library merge). runsc, sandboxd, runtime rootfs, config and the
# verified bootstrap fix are inherited unchanged from the base.
# Materialized context is produced by assemble-fs-overlay-df2e.sh from its
# Git archive and pinned byte-for-byte by fs-overlay-df2e-inputs.sha (plus
# -types.txt for symlinks/modes).
FROM akernel-bm1/all-in-one@sha256:c804c1afc51a9da7e7eeda6522bce20ae05db612c1bcd327e4dd447b2d22fb77
RUN rm -rf /opt/akernel-scheduler/function-proxy \
           /opt/akernel-scheduler/function-master \
           /opt/akernel-scheduler/runtime-manager \
           /opt/akernel-scheduler/function-agent
COPY fs-overlay-df2e/function-proxy /opt/akernel-scheduler/function-proxy
COPY fs-overlay-df2e/function-master /opt/akernel-scheduler/function-master
COPY fs-overlay-df2e/runtime-manager /opt/akernel-scheduler/runtime-manager
COPY fs-overlay-df2e/function-agent /opt/akernel-scheduler/function-agent
