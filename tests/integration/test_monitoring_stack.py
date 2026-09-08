"""Доставка метрик в Prometheus и защита Grafana через сессию приложения."""

import secrets
import time
from datetime import date
from uuid import uuid4

import requests
from sqlalchemy import select

from src.db.database import get_session_factory
from src.db.models import (
    ModelVersion,
    PredictionFeedback,
    PredictionHistory,
    Study,
    User,
)
from src.monitoring import read_quality_snapshot
from src.passwords import hash_password


def test_quality_uses_same_labeled_cohort_for_all_models():
    with get_session_factory()() as db:
        try:
            before = read_quality_snapshot(db)
            models = db.scalars(
                select(ModelVersion)
                .where(ModelVersion.role.in_(["champion", "challenger"]))
                .order_by(ModelVersion.id)
            ).all()
            assert len(models) == 2
            for index in range(3):
                study = Study(
                    patient_code=f"QMT{index:03d}",
                    study_date=date(2026, 9, 8),
                    features=dict(
                        pregnancies=0,
                        glucose=120,
                        blood_pressure=70,
                        skin_thickness=20,
                        insulin=0,
                        bmi=30,
                        diabetes_pedigree_function=0.5,
                        age=40,
                    ),
                )
                db.add(study)
                db.flush()
                if index != 1:
                    db.add(PredictionFeedback(study_id=study.id, true_label=1))
                for model_index, model in enumerate(models):
                    if index == 2 and model_index == 1:
                        continue
                    db.add(
                        PredictionHistory(
                            study_id=study.id,
                            model_version_id=model.id,
                            role_at_prediction=model.role,
                            prediction=model_index,
                            probability=float(model_index),
                        )
                    )
            db.flush()
            after = read_quality_snapshot(db)
            assert after["studies"] == before["studies"] + 3
            assert after["feedback"] == before["feedback"] + 2
            assert after["cohort"] == before["cohort"] + 1
            for index, model in enumerate(after["models"]):
                counts = dict(before["models"][index]["counts"])
                counts["fn" if index == 0 else "tp"] += 1
                assert model["counts"] == counts
        finally:
            db.rollback()


def test_monitoring_stack():
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        response = requests.get(
            "http://prometheus:9090/api/v1/query",
            params={"query": "diabetes_quality_collection_success"},
            timeout=5,
        )
        result = response.json()["data"]["result"]
        if result and result[0]["value"][1] == "1":
            break
        time.sleep(2)
    else:
        raise AssertionError("Prometheus не получил агрегаты из БД")
    assert (
        requests.get("http://frontend/grafana/api/health", timeout=10).status_code
        == 401
    )
    factory = get_session_factory()
    for role, expected in [("user", 403), ("admin", 200)]:
        username, password = "monitor_" + uuid4().hex, secrets.token_urlsafe(24)
        with factory() as db:
            db.add(
                User(
                    username=username, password_hash=hash_password(password), role=role
                )
            )
            db.commit()
        try:
            with requests.Session() as client:
                login = client.post(
                    "http://frontend/api/auth/login",
                    json={"username": username, "password": password},
                    headers={"X-Requested-With": "DiabetesPredict"},
                    timeout=10,
                )
                assert login.status_code == 200
                response = client.get("http://frontend/grafana/api/user", timeout=10)
                assert response.status_code == expected
                if role == "admin":
                    orgs = client.get(
                        "http://frontend/grafana/api/user/orgs", timeout=10
                    ).json()
                    assert any(org["role"] == "Editor" for org in orgs)
                    created = client.post(
                        "http://frontend/grafana/api/dashboards/db",
                        json={
                            "dashboard": {
                                "title": "Проверка сохранения",
                                "panels": [],
                                "schemaVersion": 39,
                            },
                            "overwrite": False,
                        },
                        timeout=10,
                    )
                    assert created.status_code == 200
                    dashboard_uid = created.json()["uid"]
                    assert (
                        client.delete(
                            "http://frontend/grafana/api/dashboards/uid/"
                            + dashboard_uid,
                            timeout=10,
                        ).status_code
                        == 200
                    )
                logout = client.post(
                    "http://frontend/api/auth/logout",
                    headers={"X-CSRF-Token": login.json()["csrf_token"]},
                    timeout=10,
                )
                assert logout.status_code == 204
                assert (
                    client.get(
                        "http://frontend/grafana/api/health", timeout=10
                    ).status_code
                    == 401
                )
        finally:
            with factory() as db:
                db.scalar(
                    select(User).where(User.username == username)
                ).is_active = False
                db.commit()


def test_container_cpu_covers_all_services():
    """cAdvisor передаёт CPU всех сервисов, включая инфраструктуру проекта."""
    expected = {
        "alloy",
        "db",
        "diabetes-api",
        "frontend",
        "grafana",
        "kafka",
        "kafka-consumer",
        "loki",
        "metrics-exporter",
        "prometheus",
        "vault",
    }
    deadline = time.monotonic() + 120
    services = set()
    while time.monotonic() < deadline:
        response = requests.get(
            "http://prometheus:9090/api/v1/query",
            params={
                "query": 'rate(container_cpu_usage_seconds_total{job="docker-containers"}[5m])'
            },
            timeout=10,
        )
        response.raise_for_status()
        samples = response.json()["data"]["result"]
        services = {
            item["metric"]["service"]
            for item in samples
            if float(item["value"][1]) >= 0
        }
        if expected <= services:
            return
        time.sleep(2)
    raise AssertionError(f"Не получен CPU сервисов: {sorted(expected - services)}")


def test_operational_metrics_and_logs_arrive():
    # Запрос без сессии даёт безопасное событие 401, не меняя исследования.
    response = requests.get("http://frontend/api/studies", timeout=10)
    assert response.status_code == 401
    correlation = response.headers["X-Request-ID"]
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        metrics = requests.get(
            "http://prometheus:9090/api/v1/query",
            params={"query": 'up{job=~"diabetes-api|diabetes-consumer|alloy|loki"}'},
            timeout=10,
        ).json()["data"]["result"]
        logs = requests.get(
            "http://loki:3100/loki/api/v1/query_range",
            params={"query": '{job="diabetes"} |= "' + correlation + '"'},
            timeout=10,
        )
        if (
            len(metrics) == 4
            and all(item["value"][1] == "1" for item in metrics)
            and logs.ok
            and logs.json()["data"]["result"]
        ):
            break
        time.sleep(2)
    else:
        raise AssertionError("Не доставлены технические метрики или журнал запросов")
    for stream in logs.json()["data"]["result"]:
        assert stream["stream"]["service"] == "api"
        assert stream["stream"]["level"] == "WARNING"
