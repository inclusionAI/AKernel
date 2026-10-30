# Build from the repository root: docker build -f builder/dind/Dockerfile .
ARG GO_BUILD_IMAGE=golang:1.25.5-bookworm
FROM ${GO_BUILD_IMAGE} AS bridge
WORKDIR /src
COPY builder/image-bridge/go.mod builder/image-bridge/go.sum ./
RUN go mod download
COPY builder/image-bridge/ ./
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -o /out/akernel-image-bridge .

FROM docker:28.3.3-dind
RUN apk add --no-cache bash python3 coreutils util-linux shadow tar xz
COPY --from=bridge /out/akernel-image-bridge /opt/akernel-image-bridge
