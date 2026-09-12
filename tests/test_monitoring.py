import json
import logging
import math
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import generate_latest

from src import metrics_exporter, monitoring
from src.auth import current_user
from src.telemetry import EventFormatter, observe_request, request_id


def test_high_recall_does_not_hide_false_positives():
    report = monitoring.quality_report(tp=10, fn=0, fp=90, tn=0)
    assert report["1"]["recall"] == 1
    assert report["1"]["precision"] == 0.1
    assert report["0"]["recall"] == 0


def test_missing_positive_examples_have_undefined_recall():
    report = monitoring.quality_report(tp=0, fn=0, fp=0, tn=5)
    assert math.isnan(report["1"]["recall"])
    assert report["0"]["recall"] == 1


@pytest.mark.parametrize("role,status", [("user", 403), ("admin", 204)])
def test_grafana_access_requires_admin(role, status):
    app = FastAPI()
    app.include_router(monitoring.router)
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=7, role=role)
    with TestClient(app) as client:
        response = client.get("/monitoring/access")
        assert response.status_code == status
        if status == 204:
            assert response.headers["X-Monitoring-User"] == "user-7"


def test_exporter_registers_only_technical_metrics(monkeypatch):
    from prometheus_client.core import GaugeMetricFamily

    class TechnicalCollector:
        def collect(self):
            yield GaugeMetricFamily(
                "diabetes_container_memory_bytes", "Memory", value=1
            )

    server = MagicMock()
    monkeypatch.setattr(metrics_exporter, "ContainerCollector", TechnicalCollector)
    monkeypatch.setattr(metrics_exporter, "configure_diagnostics", lambda: None)
    monkeypatch.setattr(metrics_exporter, "start_http_server", server)
    monkeypatch.setattr(metrics_exporter.threading, "Event", MagicMock())
    metrics_exporter.main()
    output = generate_latest(server.call_args.kwargs["registry"]).decode()
    assert "diabetes_container_memory_bytes 1.0" in output
    assert "diabetes_quality" not in output
    assert "diabetes_model_health" not in output


def test_report_matches_sklearn_on_expanded_observations():
    from sklearn.metrics import classification_report

    actual = [0] * 9 + [1] * 11
    predicted = [0] * 7 + [1] * 2 + [0] * 3 + [1] * 8
    expected = classification_report(actual, predicted, output_dict=True)
    report = monitoring.quality_report(tp=8, fn=3, fp=2, tn=7)
    for name, row in expected.items():
        assert report[name] == pytest.approx(row)


def test_empty_report_does_not_claim_perfect_quality():
    report = monitoring.quality_report(tp=0, fn=0, fp=0, tn=0)
    assert math.isnan(report["accuracy"])
    assert report["1"]["support"] == 0
    assert math.isnan(report["1"]["recall"])


def test_event_formatter_excludes_private_payload_and_exception():
    record = logging.LogRecord("test", logging.ERROR, "", 0, "request_failed", (), None)
    record.password = "secret-password"
    record.features = {"glucose": 148}
    record.error_type = "ValueError"
    record.patient_code = "PAT123"
    token = request_id.set("a" * 32)
    try:
        output = EventFormatter().format(record)
    finally:
        request_id.reset(token)
    assert json.loads(output)["request_id"] == "a" * 32
    assert json.loads(output)["error_type"] == "ValueError"
    for private in ("secret-password", "glucose", "PAT123"):
        assert private not in output


def test_http_metrics_use_route_template_and_unique_request_ids(monkeypatch):
    from src import telemetry

    captured = []
    monkeypatch.setattr(
        telemetry, "event", lambda action, **fields: captured.append(fields)
    )
    app = FastAPI()
    app.middleware("http")(observe_request)

    @app.get("/studies/{patient}")
    def detail(patient: str):
        return {"ok": True}

    with TestClient(app) as client:
        first = client.get("/studies/PAT123?password=private")
        second = client.get("/studies/ABC456")
    assert first.headers["X-Request-ID"] != second.headers["X-Request-ID"]
    assert captured[0]["route"] == "/studies/{patient}"
    assert "PAT123" not in str(captured)
    assert "private" not in str(captured)
