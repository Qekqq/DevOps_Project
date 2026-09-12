# Rebuild only the backend: upstream 13.2.1 still embeds vulnerable Go modules.
# Keep the official frontend and signed datasource plugins. See SECURITY.md.
FROM golang:1.26.7-alpine@sha256:28d89ee9cc0ff9fec75c82ca201e6bf7fdf9a679d4b7b24dfa04f2bb766bb468 AS backend
RUN apk add --no-cache make git gcc musl-dev
ADD --checksum=sha256:4b410340987a5d8090d5b39d9312095b1009f20f2e27c9a50f65da6cf74a9962 \
    https://codeload.github.com/grafana/grafana/tar.gz/refs/tags/v13.2.1 /tmp/grafana.tar.gz
RUN mkdir /src && tar -xzf /tmp/grafana.tar.gz -C /src
WORKDIR /src/grafana-13.2.1
RUN go work edit -replace=google.golang.org/grpc=google.golang.org/grpc@v1.83.2 \
    && go work edit -replace=github.com/apache/thrift=github.com/apache/thrift@v0.24.0
ENV GOMAXPROCS=2
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    make build-go GO_BUILD_TAGS=oss BUILD_VERSION=13.2.1-security.1 \
    COMMIT_SHA=v13.2.1 BUILD_BRANCH=security SOURCE_DATE_EPOCH=0

FROM grafana/grafana:13.2.1-slim@sha256:2b2ca79d6fcd9b0f5d16dc8c839b46c93d2a85e72f4cef35ae4c4be0ebe65dfc AS plugins
ARG TARGETARCH
# The release pipeline and production VPS target linux/amd64.
RUN test "$TARGETARCH" = amd64
# Use signed upstream release assets pinned by SHA256. The catalog's CDN
# can be unavailable, and its default version may lag upstream releases.
ADD --chown=472:0 --checksum=sha256:982f2026325901971951de0f1a365f8cfee8ed533b1cddde8826f2fd6b1e6a5c \
    https://github.com/grafana/grafana-loki-datasource/releases/download/v13.2.0/loki-13.2.0.linux_amd64.zip /tmp/loki.zip
ADD --chown=472:0 --checksum=sha256:33c5316c52e8745e38ea844d5357723c0ebe15ce28cdaf6000cdffe95a01af72 \
    https://github.com/grafana/grafana-prometheus-datasource/releases/download/v13.1.9/prometheus-13.1.9.linux_amd64.zip /tmp/prometheus.zip
RUN unzip -q /tmp/loki.zip -d /tmp/plugins \
    && unzip -q /tmp/prometheus.zip -d /tmp/plugins

FROM grafana/grafana:13.2.1-slim@sha256:2b2ca79d6fcd9b0f5d16dc8c839b46c93d2a85e72f4cef35ae4c4be0ebe65dfc

# Install at build time: the scanned release works without downloading plugins
# during deployment. Grafana still verifies the publisher's plugin signature.
USER root
RUN apk upgrade --no-cache \
    && mkdir -p /opt/grafana-plugins && chown 472:0 /opt/grafana-plugins
USER 472
COPY --from=backend --chown=472:0 /src/grafana-13.2.1/bin/linux/amd64/grafana /usr/share/grafana/bin/grafana
COPY --from=plugins --chown=472:0 /tmp/plugins /opt/grafana-plugins
ENV GF_PATHS_PLUGINS=/opt/grafana-plugins \
    GF_PLUGINS_PREINSTALL_DISABLED=true \
    GF_PLUGINS_PREINSTALL_AUTO_UPDATE=false
