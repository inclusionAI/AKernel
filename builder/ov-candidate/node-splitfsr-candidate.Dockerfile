FROM akernel-bm1/all-in-one@sha256:6c8054ed6e6fee0f81468e766735b64b3487e62cdddc6140e178f7eff566f33d
# Node-candidate: replace ONLY the runsc and sandboxd binaries. The base is
# the CURRENT node-role image (SIGRTMIN+3 stop signal for the systemd PID1
# and every other config byte stay untouched). File modes 0755 come from the
# build context; the legacy builder has no --chmod.
COPY runsc /usr/local/bin/runsc
COPY sandboxd /usr/local/bin/sandboxd
