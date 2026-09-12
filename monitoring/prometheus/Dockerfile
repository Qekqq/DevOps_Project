# Grafana uses the HTTP API; the separate built-in Prometheus UI is not embedded.
# Rebuild both binaries from the same release with patched gRPC and Go.
FROM golang:1.26.7-alpine@sha256:28d89ee9cc0ff9fec75c82ca201e6bf7fdf9a679d4b7b24dfa04f2bb766bb468 AS build
RUN apk add --no-cache git
ADD --checksum=sha256:9969e6fa7826bed553d34fc5408f1f8cf134a8415385781078a523178302ea44 \
    https://codeload.github.com/prometheus/prometheus/tar.gz/refs/tags/v3.13.3 /tmp/source.tar.gz
RUN mkdir /src && tar -xzf /tmp/source.tar.gz -C /src
WORKDIR /src/prometheus-3.13.3
RUN go work edit -replace=google.golang.org/grpc=google.golang.org/grpc@v1.83.2
ENV GOMAXPROCS=2 CGO_ENABLED=0
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    go build -trimpath -tags netgo -ldflags "-s -w -X github.com/prometheus/common/version.Version=3.13.3-security.1" -o /out/prometheus ./cmd/prometheus \
    && go build -trimpath -tags netgo -ldflags "-s -w -X github.com/prometheus/common/version.Version=3.13.3-security.1" -o /out/promtool ./cmd/promtool

FROM prom/prometheus:v3.13.3@sha256:6976aa8a60fec930796ce5772b8d12da7a318a5daa8d40d69c5c7819a05eeed7
COPY --from=build /out/prometheus /bin/prometheus
COPY --from=build /out/promtool /bin/promtool
