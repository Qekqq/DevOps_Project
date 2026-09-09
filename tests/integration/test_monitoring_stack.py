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
from src.model_health import month_window, read_model_health
from src.model_health_history import read_model_health_history
from src.monitoring import read_quality_snapshot
from src.passwords import hash_password


def test_quality_uses_each_models_own_labeled_predictions():
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
            assert after["cohort"] == before["cohort"] + 2
            for index, model in enumerate(after["models"]):
                counts = dict(before["models"][index]["counts"])
                counts["fn" if index == 0 else "tp"] += 2 if index == 0 else 1
                assert model["counts"] == counts
        finally:
            db.rollback()


def test_monthly_model_health_uses_study_dates_and_counts_inputs_once():
    today = date(2026, 9, 9)
    start, end = month_window(today)
    with get_session_factory()() as db:
        try:
            before = read_model_health(db, today)
            # Начало включено, конец исключён; нет прогнозов и обратной связи.
            for index, study_date in enumerate([start, today, end]):
                db.add(
                    Study(
                        patient_code=f"DMT{index:03d}",
                        study_date=study_date,
                        features=dict(
                            pregnancies=0,
                            glucose=0,
                            blood_pressure=70,
                            skin_thickness=20,
                            insulin=0,
                            bmi=30,
                            diabetes_pedigree_function=0.5,
                            age=40,
                        ),
                    )
                )
            db.flush()
            after = read_model_health(db, today)
            assert after["quality"]["samples"] == before["quality"]["samples"] + 2
            assert (
                after["quality"]["glucose_zeros"]
                == before["quality"]["glucose_zeros"] + 2
            )
            assert after["quality"]["glucose_missing"] == 0
            assert after["labeled"] == before["labeled"]
            assert after["evaluated"] == before["evaluated"]
        finally:
            db.rollback()


def test_model_history_groups_study_dates_and_recalculates_monthly_quality():
    """Поздно внесённые исследования попадают в свои дни, не в дату импорта."""
    with get_session_factory()() as db:
        try:
            models = db.scalars(
                select(ModelVersion)
                .where(ModelVersion.role.in_(["champion", "challenger"]))
                .order_by((ModelVersion.role == "champion").desc(), ModelVersion.id)
            ).all()
            for index, (study_date, prediction) in enumerate(
                [
                    (date(2001, 3, 29), 1),
                    (date(2001, 3, 30), 1),
                    (date(2001, 3, 31), 0),
                    (date(2001, 3, 31), 0),
                    (date(2001, 4, 1), 1),
                    (date(2001, 4, 3), 1),
                ]
            ):
                study = Study(
                    patient_code=f"HMT{index:03d}",
                    study_date=study_date,
                    features=dict(
                        pregnancies=0,
                        glucose=0,
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
                db.add(PredictionFeedback(study_id=study.id, true_label=1))
                for model in models:
                    db.add(
                        PredictionHistory(
                            study_id=study.id,
                            model_version_id=model.id,
                            role_at_prediction=model.role,
                            prediction=prediction,
                            probability=float(prediction),
                        )
                    )
            db.flush()
            filters = dict(date_from=date(2001, 3, 30), date_to=date(2001, 4, 2))
            daily = read_model_health_history(db, **filters)
            monthly = read_model_health_history(db, group_by="month", **filters)
            selected = read_model_health_history(
                db, model_version=models[1].model_version, **filters
            )
            assert len(daily["models"]) == len(models)
            assert len(selected["models"]) == 1
            assert selected["models"][0] == daily["models"][1]
            for day_model, month_model in zip(daily["models"], monthly["models"]):
                assert day_model["summary"] == month_model["summary"]
                total = day_model["summary"]
                assert total["data_quality"]["samples"] == 4
                assert total["data_quality"]["missing_total"] == 8
                assert total["prediction_quality"]["metrics"]["accuracy"] == 0.5
                assert total["prediction_quality"]["confusion_matrix"] == {
                    "tp": 2,
                    "tn": 0,
                    "fp": 0,
                    "fn": 2,
                }
                assert [p["data_quality"]["samples"] for p in day_model["points"]] == [
                    1,
                    2,
                    1,
                    0,
                ]
                assert [
                    p["prediction_quality"]["metrics"]["accuracy"]
                    for p in day_model["points"]
                ] == [1, 0, 1, None]
                # Март: 1 верный из 3, а не среднее дневных Accuracy (1+0)/2.
                assert (
                    month_model["points"][0]["prediction_quality"]["metrics"][
                        "accuracy"
                    ]
                    == 1 / 3
                )
                assert all(
                    p["data_drift"]["psi"]["glucose"] is None
                    for p in day_model["points"]
                )
                assert day_model["points"][-1]["data_quality"]["missing_total"] is None
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
