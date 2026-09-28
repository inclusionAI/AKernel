FROM akernel-bm1/all-in-one@sha256:d307f76990fdff27770904b9aef6d2cdf8358dfd26828b2ebbb03aca321758b0
# Restore-ceiling admission candidate for the FRONTEND role. The
# frontend's actually-running image is canon-df2e-yr-signalfix-term
# (d307f769..., SIGTERM) — NOT term-m1 (that rollout updated only the
# master), so the frontend candidate is based on ITS real base. Same
# four-program replacement (real /opt paths + bin/*.wheel copies);
# wrappers/sandboxd/runsc/RRT/yr/plugins/configs untouched and the
# runtime config (STOPSIGNAL SIGTERM) inherited unchanged and verified
# equal to the base. Mode 0755 from the context tree.
COPY rootfs/ /
