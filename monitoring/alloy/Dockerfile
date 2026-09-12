# Preserve upstream UI, generated components and build tags; update gRPC only.
FROM golang:1.26.7-alpine@sha256:28d89ee9cc0ff9fec75c82ca201e6bf7fdf9a679d4b7b24dfa04f2bb766bb468 AS toolchain
FROM grafana/alloy-build-image:v0.1.35@sha256:9fa2a341b53503ce42cf9900c401d689a68ea67cdec6a20f53d72e3665fb8dc6 AS build
# Avoid downloading a second Go toolchain into the module cache.
COPY --from=toolchain /usr/local/go /opt/go-1.26.7
ENV GOTOOLCHAIN=local GOROOT=/opt/go-1.26.7
ENV PATH=/opt/go-1.26.7/bin:$PATH
ADD --checksum=sha256:3906c641ccfa03ad2ff777618c13ca3a0ee8417f995d192b5912d310aa5a34a4 \
    https://codeload.github.com/grafana/alloy/tar.gz/refs/tags/v1.19.2 /tmp/source.tar.gz
RUN mkdir /src && tar -xzf /tmp/source.tar.gz -C /src
WORKDIR /src/alloy-1.19.2
RUN go version | grep 'go1.26.7 ' \
    && go mod edit -replace=google.golang.org/grpc=google.golang.org/grpc@v1.83.2 \
    && cd collector && go mod edit -replace=google.golang.org/grpc=google.golang.org/grpc@v1.83.2
ENV GOMAXPROCS=2 GOFLAGS=-mod=mod
ARG BUILD_GODEBUG
RUN --mount=type=cache,target=/root/.npm,sharing=locked \
    cd internal/web/ui && npm ci --no-audit --no-fund && npm run build
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    GODEBUG=${BUILD_GODEBUG} RELEASE_BUILD=1 VERSION=v1.19.2-security.1 \
    GO_TAGS="netgo embedalloyui promtail_journal_enabled" \
    SKIP_UI_BUILD=1 SKIP_CODE_GENERATION=1 make alloy

FROM grafana/alloy:v1.19.2@sha256:b8ec653c44235fbe910879145dac3597d66b0aaecf60bcbbe82580767771a839
COPY --from=build --chown=473:473 /src/alloy-1.19.2/build/alloy /bin/alloy
