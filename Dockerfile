FROM python:3.11.16-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84 AS dependencies

COPY requirements-runtime.txt /tmp/requirements-runtime.txt
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements-runtime.txt && \
    /opt/venv/bin/pip uninstall -y pip setuptools wheel
COPY src/ /runtime/src/
RUN rm /runtime/src/train.py /runtime/src/data_preprocessing.py && \
    echo 'from src.clinical_features_v1 import ClinicalFeatures' > /runtime/src/pipelines.py

FROM python:3.11.16-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84

WORKDIR /app
RUN /usr/local/bin/python -m pip uninstall -y pip setuptools wheel

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/opt/venv/bin:$PATH" \
    HOME=/tmp \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1 \
    DO_NOT_TRACK=1

COPY --from=dependencies /opt/venv /opt/venv
RUN chmod -R a-w /opt/venv
COPY --from=dependencies /runtime/src/ ./src/
# Training provenance is delivered alongside models and validated at registration.
COPY config.ini runtime-logging.json ./
COPY db/ ./db/
COPY data/raw/ ./data/raw/
COPY scripts/update_database.py scripts/activate_model_release.py scripts/create_user.py scripts/provision_metrics_user.py scripts/provision_runtime_users.py scripts/database_maintenance.py ./scripts/
RUN groupadd --gid 10001 mluser && useradd --uid 10001 --gid 10001 --no-create-home mluser && \
    mkdir -p /app/logs /app/data/feedback /app/models && \
    chown 10001:10001 /app/logs /app/data/feedback && \
    chmod -R a-w /app/src

USER 10001:10001

EXPOSE 8000

CMD ["uvicorn", "src.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--log-config", "runtime-logging.json", "--limit-concurrency", "32", "--timeout-keep-alive", "5"]
