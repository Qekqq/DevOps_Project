from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from src import study_editing as editing


@pytest.fixture
def setup(monkeypatch):
    features = dict(
        pregnancies=6,
        glucose=148,
        blood_pressure=72,
        skin_thickness=35,
        insulin=0,
        bmi=33.6,
        diabetes_pedigree_function=0.627,
        age=50,
    )
    original = dict(patient_code="PAT001", study_date="2026-09-08", **features)
    study = SimpleNamespace(id=3, features=features)
    db = MagicMock()
    db.scalar.return_value = None
    models = [
        SimpleNamespace(id=1, role="champion"),
        SimpleNamespace(id=2, role="challenger"),
    ]
    saved = [
        SimpleNamespace(
            model_version_id=m.id,
            prediction=0,
            probability=0.1,
            role_at_prediction=m.role,
        )
        for m in models
    ]
    db.scalars.return_value.all.side_effect = [saved, models]
    registry = MagicMock()
    registry.from_record.return_value.predict.return_value = {
        "prediction": 1,
        "probability": 0.8,
        "label": "detected",
    }
    monkeypatch.setattr(editing, "registry", registry)
    monkeypatch.setattr(editing, "get_study", lambda *args: study)
    feedback = MagicMock()
    monkeypatch.setattr(editing, "save_prediction_feedback", feedback)
    return db, study, original, saved, registry, feedback


def save(setup, **changes):
    db, _, original, *_ = setup
    payload = dict(input={**original, **changes}, original=original)
    return editing.update_study(
        db,
        SimpleNamespace(id=1),
        "PAT001",
        date(2026, 9, 8),
        editing.StudyUpdate(**payload),
    )


def test_one_changed_indicator_recalculates_both_models_and_records_audit(setup):
    db, study, _, saved, registry, _ = setup
    result = save(setup, glucose=160)
    assert result["recalculated"] is True
    assert registry.from_record.return_value.predict.call_count == 2
    assert study.features["glucose"] == 160
    assert all(row.probability == 0.8 for row in saved)
    audit = db.add.call_args.args[0]
    assert audit.features_before["glucose"] == 148
    assert audit.predictions_before[0]["probability"] == 0.1
    db.commit.assert_called_once()


def test_unchanged_indicators_do_not_run_models(setup):
    result = save(setup)
    assert result["recalculated"] is False
    setup[4].from_record.assert_not_called()


def test_model_failure_keeps_features_predictions_and_feedback_unchanged(setup):
    db, study, _, saved, registry, feedback = setup
    registry.from_record.return_value.predict.side_effect = [
        {"prediction": 1, "probability": 0.8, "label": "detected"},
        RuntimeError("failed"),
    ]
    with pytest.raises(HTTPException) as error:
        save(setup, glucose=160)
    assert error.value.status_code == 503
    assert study.features["glucose"] == 148
    assert all(row.probability == 0.1 for row in saved)
    db.add.assert_not_called()
    db.commit.assert_not_called()
    feedback.assert_not_called()


def test_stale_card_cannot_overwrite_newer_values(setup):
    setup[1].features = {**setup[1].features, "glucose": 170}
    with pytest.raises(HTTPException) as error:
        save(setup, glucose=160)
    assert error.value.status_code == 409
    setup[4].from_record.assert_not_called()


def test_outcome_only_does_not_recalculate(setup):
    db, _, original, _, registry, feedback = setup
    data = editing.StudyUpdate(input=original, original=original, true_label=1)
    result = editing.update_study(
        db, SimpleNamespace(id=1), "PAT001", date(2026, 9, 8), data
    )
    assert result["recalculated"] is False
    registry.from_record.assert_not_called()
    feedback.assert_called_once()


def test_identity_cannot_change(setup):
    with pytest.raises(HTTPException) as error:
        save(setup, patient_code="PAT002")
    assert error.value.status_code == 422
    setup[0].commit.assert_not_called()
