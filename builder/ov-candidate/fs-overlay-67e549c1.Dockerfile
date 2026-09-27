# Snapshot-protocol candidate 67e549c1: four-program tree overlay on the
# verified ov24c immutable manifest. Same assembly as fs-overlay.Dockerfile
# (8eb8d13a line): each program directory carries its own binary plus the
# COMPLETE formal-output library set, replacing the whole directory so no
# stale library can survive an additive COPY. The materialized tree under
# fs-overlay-67e549c1/ is produced by assemble-fs-overlay-67e549c1.sh and
# pinned byte-for-byte by fs-overlay-67e549c1-inputs.sha (340 entries).
# rootfs, runsc, sandboxd, config and the verified bootstrap-path fix of the
# ov24c base are inherited unchanged — this overlay adds NO other layer.
FROM akernel-bm1/all-in-one@sha256:aceebf14e7d2ef241650cfe6d2ab6a0c4306cba8511b911a56159ed11cf35427
RUN rm -rf /opt/akernel-scheduler/function-proxy \
           /opt/akernel-scheduler/function-master \
           /opt/akernel-scheduler/runtime-manager \
           /opt/akernel-scheduler/function-agent
COPY fs-overlay-67e549c1/function-proxy /opt/akernel-scheduler/function-proxy
COPY fs-overlay-67e549c1/function-master /opt/akernel-scheduler/function-master
COPY fs-overlay-67e549c1/runtime-manager /opt/akernel-scheduler/runtime-manager
COPY fs-overlay-67e549c1/function-agent /opt/akernel-scheduler/function-agent
