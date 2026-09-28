FROM akernel-bm1/all-in-one@sha256:28e6e5fb707783feb1d90725a33983b71f5bbd7d6bb337e2a194614b19b0c266
# Restore-ceiling admission candidate (functionsystem 72da2ffd, formal
# Release independently passed 2026-09-28) for the SIGTERM roles
# (master/frontend). Same four-program replacement as the node v3
# recipe; wrappers/sandboxd/runsc/RRT/yr/plugins/configs untouched and
# the runtime config (incl. STOPSIGNAL SIGTERM) inherited unchanged and
# verified equal to the base. Mode 0755 from the context tree.
COPY rootfs/ /
