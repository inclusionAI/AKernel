FROM akernel-bm1/all-in-one@sha256:1bb51e0ec78b3e0932d48848ab68b3b937052b0796dc4c6af844fcdb2cf50d07
# Restore-ceiling admission candidate (functionsystem 72da2ffd, formal
# Release independently passed 2026-09-28). Replaces the FOUR scheduler
# programs at their REAL /opt runtime paths AND the faasfrontend wheel
# copies under /home/yuanrong/functionsystem/bin/*.wheel. The sh
# wrappers, sandboxd, runsc, RRT, yr, plugins and every config byte of
# the v2 node image stay untouched; the runtime config (incl.
# SIGRTMIN+3) is inherited unchanged and verified equal to the base.
# Mode 0755 comes from the context tree (legacy builder has no --chmod).
COPY rootfs/ /
