# syntax=docker/dockerfile:1
# Transport-only API/Ingress image. Execution workers keep their qualified image.
ARG AKERNEL_BASE_IMAGE
FROM ${AKERNEL_BASE_IMAGE}
ARG AKERNEL_BASE_IMAGE
ARG ADX_RELEASE_COMMIT
ARG ADX_RELEASE_SHA256
COPY release.tar.gz /tmp/control-release.tar.gz
COPY install-control-release.py /tmp/install-control-release.py
RUN python3 /tmp/install-control-release.py \
      --archive /tmp/control-release.tar.gz --sha256 "${ADX_RELEASE_SHA256}" \
      --commit "${ADX_RELEASE_COMMIT}" \
    && rm /tmp/control-release.tar.gz /tmp/install-control-release.py
LABEL ai.akernel.image-role="ingress-api" \
      ai.akernel.base-image="${AKERNEL_BASE_IMAGE}" \
      ai.akernel.control-adx-commit="${ADX_RELEASE_COMMIT}" \
      ai.akernel.control-adx-archive-sha256="${ADX_RELEASE_SHA256}"
