# This image contains tools/configuration only. Session credentials are injected
# separately by the runtime and must never be baked into image layers.
FROM docker:28.3.3-dind
RUN apk add --no-cache python3 bash coreutils util-linux shadow tar xz ca-certificates
COPY --chmod=0755 akernel-image-bridge /opt/akernel-image-bridge
COPY image_bridge_guest.py /usr/lib/akernel/image_bridge_guest.py
COPY docker_network_guest.py /usr/lib/akernel/docker_network_guest.py
COPY compose.py /usr/lib/akernel/compose.py
RUN test -s /usr/lib/akernel/compose.py && \
    test -s /usr/lib/akernel/image_bridge_guest.py && \
    test -s /usr/lib/akernel/docker_network_guest.py && \
    python3 -m py_compile /usr/lib/akernel/*.py
