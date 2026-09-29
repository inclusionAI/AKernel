FROM akernel-bm1/all-in-one:canon-df2e-yr-signalfix-node-splitfsr-v4
# Trusted-restore physical-case fix, runsc side (gvisor d5223c6e: tmpfs
# fs-checkpoint merge tolerates conflicting-type transient entries such
# as CPython atomic-write temp files). Only /usr/local/bin/runsc changes
# relative to v4; mode 0755 from the context file.
COPY runsc /usr/local/bin/runsc
