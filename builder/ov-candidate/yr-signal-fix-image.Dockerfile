# yr CLI signal-fix image on the deployed canon-df2e-frontend-diag
# manifest: replaces exactly ONE file — /home/yuanrong/functionsystem/bin/yr
# (the CLI launcher PID1 execs) — with the 6ff3975-fixed build
# ecff370c8e6fdd4f34260a331366e8bc8c1635a8b557082dff5ea5dade54387e
# (23,457,955 bytes, go1.24.1/CGO=0/trimpath; symbols: ExecCommandUntil*,
# prepareBlockedCommand/Pdeathsig present, blockRun absent) built from the
# SAME df2e2f2a source via apps/cli/build-recipes/build-yr-cli.sh.
# Mode 0755 (as the original). Everything else inherited unchanged.
FROM akernel-bm1/all-in-one@sha256:a7addb40813a548127e793d777ccdbd1bafaa7cf003ceef7d35d57b40d37c664
COPY yr /home/yuanrong/functionsystem/bin/yr
RUN chmod 0755 /home/yuanrong/functionsystem/bin/yr
