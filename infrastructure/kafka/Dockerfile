FROM python:3.11.16-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84 AS dependencies
WORKDIR /build
COPY artifacts.lock.json fetch_artifacts.py ./
RUN python fetch_artifacts.py

FROM apache/kafka@sha256:77e3df9054047a88b520d0cc46e16696d3b22022e1d580aeccd2632df6532837
USER root
RUN apk upgrade --no-cache && \
    rm /opt/kafka/libs/jackson-*-2.21.2.jar \
       /opt/kafka/libs/jetty-*-12.0.34.jar \
       /opt/kafka/libs/jline-3.30.4.jar && \
    mkdir -p /bitnami/kafka/data && chown -R 1000:1000 /bitnami/kafka
COPY --from=dependencies /patches/ /opt/kafka/libs/
USER 1000:1000
