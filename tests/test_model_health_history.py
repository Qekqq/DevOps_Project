import math
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import model_health_history, monitoring
from src.auth import current_user
from src.model_health_history import period_buckets, present_snapshot


def test_daily_period_contains_both_dates_without_overlap():
    assert period_buckets(date(2026, 8, 31), date(2026, 9, 1)) == [
        (date(2026, 8, 31), date(2026, 9, 1)),
        (date(2026, 9, 1), date(2026, 9, 2)),
    ]
    assert len(period_buckets(date(2026, 9, 9), date(2026, 9, 9))) == 1


def test_calendar_months_are_clipped_and_include_leap_day():
    assert period_buckets(date(2023, 12, 15), date(2024, 3, 2), "month") == [
        (date(2023, 12, 15), date(2024, 1, 1)),
        (date(2024, 1, 1), date(2024, 2, 1)),
        (date(2024, 2, 1), date(2024, 3, 1)),
        (date(2024, 3, 1), date(2024, 3, 3)),
    ]


@pytest.mark.parametrize(
    "start,end,group",
    [
        (date(2026, 9, 10), date(2026, 9, 9), "day"),
        (date(2025, 1, 1), date(2026, 9, 9), "day"),
        (date(2026, 9, 9), date(2026, 9, 9), "hour"),
    ],
)
def test_invalid_period_is_rejected(start, end, group):
    with pytest.raises(ValueError):
        period_buckets(start, end, group)


def test_empty_intervals_are_gaps_and_small_drift_is_not_zero():
    from src.features import ZERO_AS_MISSING_COLUMNS

    snapshot = {
        "start": date(2026, 9, 9),
        "end": date(2026, 9, 10),
        "reference": {"samples": 536},
        "quality": {"samples": 0} | {f + "_zeros": 0 for f in ZERO_AS_MISSING_COLUMNS},
        "current_samples": 0,
        "labeled": 0,
        "evaluated": 0,
        "drift": {"glucose": math.nan},
        "target_shift": math.nan,
        "positive_rate": math.nan,
        "reference_positive_rate": 0.35,
        "classification": {"accuracy": math.nan},
        "confusion": dict.fromkeys(["tp", "tn", "fp", "fn"], 0),
    }
    empty = present_snapshot(snapshot)
    assert empty["data_quality"]["missing_total"] is None
    assert empty["prediction_quality"]["confusion_matrix"]["tp"] is None
    assert empty["prediction_quality"]["metrics"]["accuracy"] is None
    assert empty["data_drift"]["status"] == "Нет данных"
    snapshot["current_samples"] = 3
    snapshot["labeled"] = 2
    assert present_snapshot(snapshot)["data_drift"]["status"] == "Недостаточно данных"
    assert present_snapshot(snapshot)["target_drift"]["shift"] is None
    snapshot["quality"]["samples"] = 3
    assert present_snapshot(snapshot)["data_quality"]["missing_total"] == 0


def test_model_filter_does_not_change_period_and_total_is_recalculated(monkeypatch):
    models = [
        SimpleNamespace(
            id=i,
            model_version=f"v{i}",
            model_name="test",
            family="decision_tree",
            role=role,
        )
        for i, role in [(1, "champion"), (2, "challenger")]
    ]
    db = MagicMock()
    db.scalars.return_value.all.return_value = models
    # Отдельно проверяем маршрутизацию выбранных границ в расчёт, без БД.
    read = MagicMock(return_value={"reference": {"version": "v2"}})
    monkeypatch.setattr(model_health_history, "read_model_health", read)
    monkeypatch.setattr(model_health_history, "present_snapshot", lambda value: value)
    result = model_health_history.read_model_health_history(
        db, date_from=date(2026, 8, 31), date_to=date(2026, 9, 1), model_version="v2"
    )
    assert len(result["models"]) == 1
    assert len(result["available_models"]) == 2
    assert [call.kwargs["model_id"] for call in read.call_args_list] == [2, 2, 2]
    assert [(c.kwargs["start"], c.kwargs["end"]) for c in read.call_args_list] == [
        (date(2026, 8, 31), date(2026, 9, 2)),
        (date(2026, 8, 31), date(2026, 9, 1)),
        (date(2026, 9, 1), date(2026, 9, 2)),
    ]
    with pytest.raises(LookupError):
        model_health_history.read_model_health_history(
            db,
            date_from=date(2026, 9, 9),
            date_to=date(2026, 9, 9),
            model_version="bad",
        )


@pytest.fixture
def api(monkeypatch):
    app = FastAPI()
    app.include_router(monitoring.router)
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=1, role="admin")
    factory = MagicMock()
    monkeypatch.setattr(monitoring, "get_session_factory", factory)
    reader = MagicMock(return_value={"models": []})
    monkeypatch.setattr(monitoring, "read_model_health_history", reader)
    with TestClient(app) as client:
        yield app, client, factory, reader


@pytest.mark.parametrize("role,expected", [(None, 401), ("user", 403), ("admin", 200)])
def test_history_access_and_default_dates(api, role, expected):
    app, client, factory, reader = api
    if role is None:
        app.dependency_overrides.clear()
    else:
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(
            id=1, role=role
        )
    response = client.get("/monitoring/model-health")
    assert response.status_code == expected
    if expected == 200:
        assert response.headers["Cache-Control"] == "no-store"
        args = reader.call_args.kwargs
        assert (args["date_to"] - args["date_from"]).days == 29
    else:
        factory.assert_not_called()


@pytest.mark.parametrize(
    "query",
    [
        "date_from=2026-09-09",
        "date_from=2026-09-10&date_to=2026-09-09",
        "date_from=2026-09-09&date_to=2026-09-09&group_by=hour",
    ],
)
def test_bad_filters_do_not_query_database(api, query):
    _, client, factory, _ = api
    assert client.get("/monitoring/model-health?" + query).status_code == 422
    factory.assert_not_called()


def test_valid_filters_and_failure_without_private_details(api):
    _, client, _, reader = api
    path = "/monitoring/model-health?date_from=2026-08-01&date_to=2026-09-09&group_by=month&model_version=v2"
    assert client.get(path).status_code == 200
    assert reader.call_args.kwargs == {
        "date_from": date(2026, 8, 1),
        "date_to": date(2026, 9, 9),
        "group_by": "month",
        "model_version": "v2",
    }
    reader.side_effect = ValueError("private details")
    response = client.get(path)
    assert response.status_code == 503
    assert "private details" not in response.text
