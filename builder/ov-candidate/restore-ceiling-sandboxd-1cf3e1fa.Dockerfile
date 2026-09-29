FROM akernel-bm1/all-in-one:canon-df2e-yr-signalfix-node-splitfsr-v3
# Trusted-restore ceiling admission, sandboxd side (commit 1cf3e1fa:
# elastic restore keeps max(caller reserved ceiling, sidecar limit),
# clamped to the source's policy ceiling). Only /usr/local/bin/sandboxd
# changes relative to v3; mode 0755 from the context file.
COPY sandboxd /usr/local/bin/sandboxd
