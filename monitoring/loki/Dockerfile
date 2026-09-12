# Same upstream release, rebuilt with security fixes in Go and gRPC.
FROM golang:1.26.7-alpine@sha256:28d89ee9cc0ff9fec75c82ca201e6bf7fdf9a679d4b7b24dfa04f2bb766bb468 AS build
RUN apk add --no-cache git make bash
ADD --checksum=sha256:e2e8863c15ad97a4649a6f0795d549a8977e44f4413e2b001e6eb0c12c22eb9c \
    https://codeload.github.com/grafana/loki/tar.gz/refs/tags/v3.7.7 /tmp/source.tar.gz
RUN mkdir /src && tar -xzf /tmp/source.tar.gz -C /src
WORKDIR /src/loki-3.7.7
RUN go mod edit -replace=google.golang.org/grpc=google.golang.org/grpc@v1.83.2
ENV GOMAXPROCS=2 CGO_ENABLED=0 GOFLAGS=-mod=mod
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    make BUILD_IN_CONTAINER=false GOFLAGS=-mod=mod IMAGE_TAG=3.7.7-security.1 loki

FROM grafana/loki:3.7.7@sha256:d70e4659623f3e109af669cae76fe2a5dd5be54e2298fe8aed380d982fbc2500
COPY --from=build --chown=10001:10001 /src/loki-3.7.7/cmd/loki/loki /usr/bin/loki
