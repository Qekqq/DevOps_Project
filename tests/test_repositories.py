from types import SimpleNamespace
from datetime import date
from unittest.mock import Mock

import pytest

import src.db.repositories as repositories


FEATURES = {
    "pregnancies": 6, "glucose": 148, "blood_pressure": 72,
    "skin_thickness": 35, "insulin": 0, "bmi": 33.6,
    "diabetes_pedigree_function": 0.627, "age": 50,
}


def test_existing_prediction_is_not_overwritten_or_duplicated(monkeypatch):
    db = Mock()
    db.execute.return_value.scalar_one_or_none.return_value = None
    db.execute.return_value.scalar_one_or_none.side_effect = [
        SimpleNamespace(id=19, features=FEATURES),
        SimpleNamespace(prediction=1, probability=0.81, label="detected", model_version_snapshot="v1", inference_payload={
            "features": FEATURES,
            "result": {"prediction": 1, "probability": 0.81, "label": "detected"},
        })
    ]

    with pytest.raises(repositories.DuplicatePredictionError):
        repositories.save_prediction_history(
            db, features=FEATURES, prediction=1, probability=0.81,
            model_version=SimpleNamespace(id=1, model_version="v1", role="champion"),
            patient_code="pat001", study_date=date(2026, 9, 8),
        )

    db.add.assert_not_called()
    db.flush.assert_not_called()


def test_new_prediction_keeps_normalized_patient_code(monkeypatch):
    db = Mock()
    db.execute.return_value.scalar_one_or_none.return_value = None
    db.execute.return_value.scalars.return_value.all.return_value = []
    monkeypatch.setattr(repositories, "get_or_create_study", Mock(return_value=SimpleNamespace(id=19)))

    result = repositories.save_prediction_history(
        db, features=FEATURES, prediction=1, probability=0.81,
        model_version=SimpleNamespace(id=1, model_version="v1", role="champion"),
        patient_code=" pat001 ", study_date=date(2026, 9, 8),
    )

    assert result.study_id == 19
    assert result.probability == 0.81
    assert result.role_at_prediction == "champion"
    db.add.assert_called_once_with(result)
    db.commit.assert_not_called()


@pytest.mark.parametrize("code", [None, "", "ABC-123", "Aß001"])
def test_invalid_patient_code_cannot_be_saved(code):
    db = Mock()
    with pytest.raises(ValueError):
        repositories.save_prediction_history(
            db, features=FEATURES, prediction=1, probability=0.81,
            model_version=SimpleNamespace(id=1, model_version="v1", role="champion"),
            patient_code=code, study_date=date(2026, 9, 8),
        )
    db.execute.assert_not_called()
    db.add.assert_not_called()


def test_study_lookup_filters_patient_and_date():
    db = Mock()
    db.execute.return_value.scalar_one_or_none.return_value = None
    db.execute.return_value.scalars.return_value.all.return_value = []

    result = repositories.get_prediction_for_study(
        db, patient_code=" pat001 ", study_date=date(2026, 10, 8),
        model_version="v2", features=FEATURES,
    )

    assert result is None
    statement = db.execute.call_args.args[0]
    parameters = statement.compile().params
    assert "PAT001" in parameters.values()
    assert date(2026, 10, 8) in parameters.values()


def test_study_comparison_uses_original_precision():
    db = Mock()
    db.execute.return_value.scalar_one_or_none.return_value = SimpleNamespace(
        id=19, features={**FEATURES, "bmi": 33.6001},
    )

    with pytest.raises(repositories.StudyConflictError):
        repositories.get_prediction_for_study(
            db, patient_code="PAT001", study_date=date(2026, 9, 8),
            model_version="v2", features={**FEATURES, "bmi": 33.6002},
        )


def test_study_is_reused_for_another_model():
    db = Mock()
    study = SimpleNamespace(id=19, features=FEATURES)
    db.execute.return_value.scalar_one_or_none.return_value = study
    assert repositories.get_or_create_study(db, "PAT001", date(2026, 9, 8), FEATURES) is study
    db.add.assert_not_called()


def test_study_without_predictions_still_rejects_different_features():
    db = Mock()
    db.execute.return_value.scalar_one_or_none.return_value = SimpleNamespace(features=FEATURES)
    with pytest.raises(repositories.StudyConflictError):
        repositories.get_prediction_for_study(
            db, patient_code="PAT001", study_date=date(2026, 9, 8),
            model_version="v2", features={**FEATURES, "glucose": 120},
        )


def test_feedback_belongs_to_study_and_can_be_corrected():
    db = Mock()
    study = SimpleNamespace(id=19)
    db.execute.return_value.scalar_one_or_none.side_effect = [study, None]
    feedback = repositories.save_prediction_feedback(db, study_id=19, true_label=1)
    assert feedback.study_id == 19
    assert feedback.true_label == 1
    db.execute.return_value.scalar_one_or_none.side_effect = [study, feedback]
    updated = repositories.save_prediction_feedback(db, study_id=19, true_label=0)
    assert updated is feedback
    assert updated.true_label == 0
    db.add.assert_called_once_with(feedback)
    db.commit.assert_not_called()


@pytest.mark.parametrize("label", [True, 0.5, 2, "1"])
def test_invalid_feedback_is_rejected_before_database_access(label):
    db = Mock()
    with pytest.raises(ValueError):
        repositories.save_prediction_feedback(db, study_id=19, true_label=label)
    db.execute.assert_not_called()


def test_invalid_measurement_cannot_be_saved_from_consumer():
    db = Mock()
    with pytest.raises(ValueError):
        repositories.save_prediction_history(
            db, features={**FEATURES, "glucose": -1}, prediction=1, probability=0.81,
            model_version=SimpleNamespace(id=1, model_version="v1", role="champion"),
            patient_code="PAT001", study_date=date(2026, 9, 8),
        )
    db.execute.assert_not_called()
