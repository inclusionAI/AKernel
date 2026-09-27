FROM akernel-bm1/all-in-one@sha256:55749a0201641ec8326b51e2658c8075e0a9bd7cabf59b5b83e08d5d78ac190a
COPY runsc /usr/local/bin/runsc
RUN chmod 0755 /usr/local/bin/runsc && sha256sum /usr/local/bin/runsc
