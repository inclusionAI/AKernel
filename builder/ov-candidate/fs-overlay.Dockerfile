# Snapshot-protocol candidate: four-program tree overlay on the ov23
# immutable manifest. Replaces ONLY /opt/akernel-scheduler/{function-proxy,
# function-master,runtime-manager,function-agent} — complete directory
# trees (binaries + full accompanying libraries) from the formal Release
# build of functionsystem 8eb8d13a. The stale-library-merge hazard of
# additive COPY is avoided by deleting each target directory BEFORE its
# replacement tree is copied in. rootfs, runsc, sandboxd, config and the
# wrapper bytes of the ov23 base are inherited unchanged.
FROM akernel-bm1/all-in-one@sha256:2b5136f4f1d7ed42a205f54f0277d04e0345cf194755e756e4cb0379d52e474c
COPY fs-overlay/function-proxy /tmp/overlay/function-proxy
COPY fs-overlay/function-master /tmp/overlay/function-master
COPY fs-overlay/runtime-manager /tmp/overlay/runtime-manager
COPY fs-overlay/function-agent /tmp/overlay/function-agent
RUN set -e \
 && for n in function-proxy function-master runtime-manager function-agent; do \
      rm -rf "/opt/akernel-scheduler/$n"; \
      mkdir -p /opt/akernel-scheduler; \
      cp -a "/tmp/overlay/$n" "/opt/akernel-scheduler/$n"; \
    done \
 && rm -rf /tmp/overlay \
 && find /opt/akernel-scheduler -type f -exec sha256sum {} \; > /tmp/overlay.sha \
 && wc -l /tmp/overlay.sha
