# Single-file bootstrap fix for the snapshot-protocol candidate: FROM the
# verified ov24b immutable manifest, replacing ONLY the node bootstrap
# script with the checkpoint-directory-corrected version (5f7afb9d:
# --checkpoint_dir must point at sandboxd's managed root
# /home/akernel/sandboxd/root/checkpoints). Nothing else changes.
FROM akernel-bm1/all-in-one@sha256:1d219ffbdf97068cec6f6baf91c88b718805cca11b06a80ab46080db0264256c
COPY bootstrap/yr_node_bootstrap.sh /home/yuanrong/yr_node_bootstrap.sh
RUN chmod 0755 /home/yuanrong/yr_node_bootstrap.sh \
 && sha256sum /home/yuanrong/yr_node_bootstrap.sh
