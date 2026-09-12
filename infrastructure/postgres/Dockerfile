# Keep the PostgreSQL major version and Debian family of the installed cluster.
FROM postgres:16@sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94

USER root
# Install distribution security fixes before scanning the resulting immutable image.
RUN apt-get update && apt-get upgrade -y --no-install-recommends && \
    rm -rf /var/lib/apt/lists/* && \
    rm /usr/local/bin/gosu

# The official entrypoint only needs gosu when started as root. Existing data
# volumes must already belong to postgres; migration verifies ownership first.
USER postgres
