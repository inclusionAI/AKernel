# Minimal FRONTEND-ONLY diagnosis image on the deployed canon-df2e-rel
# manifest: replaces exactly ONE file — /home/yuanrong/faas/faasfrontend/
# faasfrontend.so — with the path-aligned diagnostic plugin
# 6224dd7c738240dd9ffe0e51012e7db66b076103c6a5c827375199570449c1f1
# (frontend 6113d1b recipe; ABI two-phase verified against this image's
# own goruntime). Everything else — goruntime, runsc, sandboxd, runtime
# rootfs, config, bootstrap, all four functionsystem program dirs — is
# inherited unchanged.
#
# Startup-chain evidence: the .so ships in the image (mtime = image build
# time, Sep 20) and is NOT written at pod start; the frontend goruntime
# loads it via YR_FUNCTION_LIB_PATH=/home/yuanrong/faas/faasfrontend/ and
# plugin file offset 0x20c in maps matches the on-disk file — no zip
# rewrite, no other directory to patch. faasfrontend_meta.json is
# descriptive metadata; the meta is not consulted by the loader path.
FROM akernel-bm1/all-in-one@sha256:081dd48c2886bf70255bc28d459b70edfbb6c9ad6a525fc144c174f939921e27
COPY faasfrontend.so /home/yuanrong/faas/faasfrontend/faasfrontend.so
