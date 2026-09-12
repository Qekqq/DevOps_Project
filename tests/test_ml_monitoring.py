"""Cohort semantics and the real Evidently adapter, including sparse samples."""

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import ml_monitoring, monitoring
from src.auth import current_user
from src.features import FEATURE_COLUMNS
from src.kafka.consumer import prediction_time
from src.ml_monitoring import buckets, evaluate


def samples(actual, predicted=None):
    predicted = predicted if predicted is not None else [1] * len(actual)
    return pd.DataFrame(
        [
            {
                **dict.fromkeys(FEATURE_COLUMNS, 10.0),
                "target": a,
                "prediction": p,
                "probability": 0.8 if p else 0.2,
            }
            for a, p in zip(actual, predicted)
        ],
        columns=[*FEATURE_COLUMNS, "target", "prediction", "probability"],
    )


def test_calendar_windows_not_cumulative_and_no_thirty_day_retention():
    assert buckets(date(2024, 2, 28), date(2024, 3, 1), "month") == [
        (date(2024, 2, 28), date(2024, 3, 1)),
        (date(2024, 3, 1), date(2024, 3, 2)),
    ]
    assert buckets(date(2026, 9, 6), date(2026, 9, 8), "week") == [
        (date(2026, 9, 6), date(2026, 9, 7)),
        (date(2026, 9, 7), date(2026, 9, 9)),
    ]
    assert len(buckets(date(2020, 1, 1), date(2026, 1, 1), "month")) == 73
    with pytest.raises(ValueError):
        buckets(date(2020, 1, 1), date(2026, 1, 1), "day")


def test_missing_outcomes_only_excluded_from_quality_and_zero_pregnancies_valid():
    frame = samples([1, 0, None], [1, 1, 0])
    frame["pregnancies"] = 0
    frame.loc[2, "glucose"] = 0
    report = evaluate(frame, None)
    assert report["samples"] == 3
    assert report["evaluated"] == 2
    assert report["missing_outcomes"] == 1
    assert report["metrics"]["recall"] == 1
    assert report["metrics"]["precision"] == 0.5
    assert report["missing"]["glucose"] == 1
    assert report["missing"]["pregnancies"] == 0
    assert report["drift"]["glucose"]["samples"] == 2


def test_latest_prediction_and_corrected_features_recalculate():
    frame = samples([1, 0], [0, 1])
    frame.loc[0, "glucose"] = 0
    before = evaluate(frame, None)
    frame.loc[0, ["prediction", "probability", "glucose"]] = [1, 0.9, 120]
    after = evaluate(frame, None)
    assert before["metrics"]["recall"] == 0
    assert after["metrics"]["recall"] == 1
    assert before["missing"]["glucose"] == 1
    assert after["missing"]["glucose"] == 0
    # The stored class must win over probability thresholding.
    frame.loc[0, "prediction"] = 0
    assert evaluate(frame, None)["metrics"]["recall"] == 0


@pytest.mark.parametrize("actual,predicted", [([], []), ([None], [1]), ([0], [0])])
def test_undefined_metrics_are_gaps(actual, predicted):
    report = evaluate(samples(actual, predicted), None)
    assert report["metrics"]["recall"] is None
    assert report["metrics"]["roc_auc"] is None
    assert all(d["psi"] is None for d in report["drift"].values())


def test_psi_detects_shift_and_needs_valid_values_in_both_samples():
    reference = samples([None] * 110)
    reference["glucose"] = np.linspace(80, 120, 110)
    current = reference.copy()
    assert evaluate(current, reference)["drift"]["glucose"]["psi"] == pytest.approx(0)
    current["glucose"] += 200
    shifted = evaluate(current, reference)["drift"]["glucose"]
    assert shifted["detected"] is True
    current.loc[:20, "glucose"] = 0
    assert evaluate(current, reference)["drift"]["glucose"]["psi"] is None


def test_prediction_event_time_survives_kafka_delivery():
    assert prediction_time({"created_at": "2026-07-01T02:00:00+03:00"}) == datetime(
        2026, 6, 30, 23, tzinfo=timezone.utc
    )
    assert prediction_time({}) is None
    with pytest.raises(ValueError):
        prediction_time({"created_at": "2026-07-01T02:00:00"})


def test_numeric_report_never_uses_nltk_model_artifact_apis(monkeypatch):
    """Regression boundary for GHSA-8mgp-746c-j5xp, not a package-level fix."""
    from nltk.classify import maxent
    from nltk.parse.transitionparser import TransitionParser
    from nltk.tag.perceptron import AveragedPerceptron, PerceptronTagger

    def forbidden(*args, **kwargs):
        pytest.fail("Numeric monitoring must not access NLTK model artifacts")

    for owner, methods in [
        (TransitionParser, ["train", "parse"]),
        (AveragedPerceptron, ["save", "load"]),
        (PerceptronTagger, ["save_to_json", "load_from_json"]),
        (maxent, ["save_maxent_params", "load_maxent_params"]),
    ]:
        for name in methods:
            monkeypatch.setattr(owner, name, forbidden)
    frame = samples([0, 1] * 60, [0, 1] * 60)
    result = evaluate(frame, frame)
    assert result["metrics"]["accuracy"] == 1
    assert result["metrics"]["roc_auc"] == 1
    assert result["drift"]["glucose"]["psi"] == 0


def test_report_api_auth_defaults_and_slot_release(monkeypatch):
    app = FastAPI()
    app.include_router(monitoring.router)
    reader = MagicMock(return_value={"summary": {}})
    factory = MagicMock()
    monkeypatch.setattr(monitoring, "read_report", reader)
    monkeypatch.setattr(monitoring, "get_session_factory", factory)
    with TestClient(app) as client:
        assert client.get("/monitoring/model-report").status_code == 401
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(
            id=1, role="user"
        )
        assert client.get("/monitoring/model-report").status_code == 403
        factory.assert_not_called()
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(
            id=1, role="admin"
        )
        response = client.get("/monitoring/model-report")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert reader.call_args.kwargs["date_from"] is None
        assert reader.call_args.kwargs["time_axis"] == "prediction_time"
        assert reader.call_args.kwargs["resolution"] == "month"
        assert (
            client.get("/monitoring/model-report?time_axis=feedback_date").status_code
            == 422
        )
        assert monitoring.report_slots.acquire(blocking=False)
        try:
            assert client.get("/monitoring/model-report").status_code == 503
        finally:
            monitoring.report_slots.release()
        reader.side_effect = TimeoutError("timeout")
        assert client.get("/monitoring/model-report").status_code == 503
        reader.side_effect = None
        assert client.get("/monitoring/model-report").status_code == 200


def test_database_cohort_uses_selected_axis_and_latest_rows(monkeypatch):
    model = SimpleNamespace(id=5, model_version="v5", training_run_id=1)
    db = MagicMock()
    db.scalar.return_value = model
    start = datetime(2026, 7, 1, 22, tzinfo=timezone.utc)
    end = datetime(2026, 7, 2, 22, tzinfo=timezone.utc)

    def study(i):
        return SimpleNamespace(
            id=i,
            study_date=date(2020, 1, i),
            features=dict.fromkeys(FEATURE_COLUMNS, 10.0),
        )

    rows = [
        (SimpleNamespace(created_at=start, prediction=1, probability=0.8), study(1), 1),
        (SimpleNamespace(created_at=end, prediction=0, probability=0.2), study(2), 1),
    ]
    db.execute.return_value.one.return_value = (start, end)
    db.execute.return_value.all.return_value = rows
    monkeypatch.setattr(ml_monitoring, "model_options", lambda _: [{"version": "v5"}])
    monkeypatch.setattr(ml_monitoring, "load_reference", lambda *_: (None, set(), None))
    report = ml_monitoring.read_report(db, resolution="day")
    assert [p["metrics"]["recall"] for p in report["points"]] == [1, 0]
    assert report["summary"]["metrics"]["recall"] == 0.5
    assert report["date_from"] == date(2026, 7, 1)
    rows[1][0].prediction = 1
    rows[1][0].probability = 0.9
    changed = ml_monitoring.read_report(db, resolution="day")
    assert changed["summary"]["metrics"]["recall"] == 1
    assert [p["start"] for p in changed["points"]] == [
        p["start"] for p in report["points"]
    ]
