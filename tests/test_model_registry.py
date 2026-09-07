from hashlib import sha256
from unittest.mock import Mock

import pytest

from src.config import get_project_root
from src.features import RAW_TO_CANONICAL_COLUMNS
from src.model_registry import ModelRegistry
import src.model_registry as registry_module


MEDIANS = {"glucose": 117, "blood_pressure": 72, "skin_thickness": 30, "insulin": 126, "bmi": 32.4}
FEATURES = {"pregnancies": 6, "glucose": 148, "blood_pressure": 72,
            "skin_thickness": 35, "insulin": 0, "bmi": 33.6,
            "diabetes_pedigree_function": 0.627, "age": 50}


def metadata(directory):
    path = get_project_root() / "experiments" / directory / "model.joblib"
    return dict(version=directory + "-v1", artifact_path=str(path),
                artifact_sha256=sha256(path.read_bytes()).hexdigest(), train_medians=MEDIANS)


@pytest.mark.parametrize("directory", [
    "decision_tree", "decision_tree_tuned", "logistic_regression", "logistic_regression_tuned",
])
def test_existing_models_can_predict_with_registered_preprocessing(directory):
    registry = ModelRegistry()
    predictor = registry.get_predictor(**metadata(directory))
    result = predictor.predict(FEATURES)
    assert result["prediction"] in (0, 1)
    assert 0 <= result["probability"] <= 1
    prepared = predictor.prepare_input(FEATURES)
    assert list(prepared.columns) == list(predictor.model.feature_names_in_)
    canonical = prepared.rename(columns=RAW_TO_CANONICAL_COLUMNS)
    assert canonical.loc[0, "insulin"] == MEDIANS["insulin"]
    for column, value in FEATURES.items():
        assert canonical.loc[0, column] == (MEDIANS[column] if column in MEDIANS and value == 0 else value)
    assert registry.get_predictor(**metadata(directory)) is predictor


def test_checksum_mismatch_prevents_loading(monkeypatch):
    loader = Mock()
    monkeypatch.setattr(registry_module, "DiabetesPredictor", loader)
    with pytest.raises(ValueError):
        ModelRegistry().get_predictor(**{**metadata("best_model"), "artifact_sha256": "0" * 64})
    loader.assert_not_called()


def test_registered_version_cannot_change_preprocessing():
    registry = ModelRegistry()
    entry = metadata("best_model")
    registry.get_predictor(**entry)
    with pytest.raises(ValueError):
        registry.get_predictor(**{**entry, "train_medians": {**MEDIANS, "insulin": 999}})
