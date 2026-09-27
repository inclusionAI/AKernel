# Snapshot-protocol candidate: four-program tree overlay on the ov23
# immutable manifest. Replaces ONLY /opt/akernel-scheduler/{function-proxy,
# function-master,runtime-manager,function-agent} — complete directory
# trees (binaries + full accompanying libraries) from the formal Release
# build of functionsystem 8eb8d13a. The stale-library-merge hazard of
# additive COPY is avoided by deleting each target directory BEFORE its
# replacement tree is copied in. rootfs, runsc, sandboxd, config and the
# wrapper bytes of the ov23 base are inherited unchanged.
FROM akernel-bm1/all-in-one@sha256:2b5136f4f1d7ed42a205f54f0277d04e0345cf194755e756e4cb0379d52e474c
# remove the four target trees FIRST: each following COPY then writes its
# complete replacement tree directly, in one layer each, with no staging
# duplication and no stale-library merge; digests are recorded OUTSIDE the
# image (evidence), nothing extra is written into the running image
RUN rm -rf /opt/akernel-scheduler/function-proxy \
           /opt/akernel-scheduler/function-master \
           /opt/akernel-scheduler/runtime-manager \
           /opt/akernel-scheduler/function-agent
COPY fs-overlay/function-proxy /opt/akernel-scheduler/function-proxy
COPY fs-overlay/function-master /opt/akernel-scheduler/function-master
COPY fs-overlay/runtime-manager /opt/akernel-scheduler/runtime-manager
COPY fs-overlay/function-agent /opt/akernel-scheduler/function-agent
