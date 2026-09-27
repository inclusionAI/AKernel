# Minimal FRONTEND-ONLY diagnosis image on the deployed canon-df2e-rel
# manifest: replaces exactly ONE file — /home/yuanrong/faas/faasfrontend/
# faasfrontend.so — with the path-aligned diagnostic plugin
# 6224dd7c738240dd9ffe0e51012e7db66b076103c6a5c827375199570449c1f1
# (frontend 6113d1b recipe; ABI two-phase verified against this image's
# own goruntime). Everything else — goruntime, runsc, sandboxd, runtime
# rootfs, config, bootstrap, all four functionsystem program dirs — is
# inherited unchanged.
#
# Startup-chain observations (scoped, not guarantees): the live frontend
# goruntime maps /home/yuanrong/faas/faasfrontend/faasfrontend.so (the
# 00:20c field in maps is the DEVICE number, not a file offset) and its
# YR_FUNCTION_LIB_PATH points at this exact directory; the on-disk mtime
# matches the image build time. These prove the RUNNING process uses this
# path — they do NOT alone prove no startup rewrite (a cp can preserve
# mtime); the startup/prepare chain still needs reading, and after any
# deployment the actually-mapped bytes must be re-verified against the
# full 6224dd7c... SHA.
FROM akernel-bm1/all-in-one@sha256:081dd48c2886bf70255bc28d459b70edfbb6c9ad6a525fc144c174f939921e27
COPY faasfrontend.so /home/yuanrong/faas/faasfrontend/faasfrontend.so
