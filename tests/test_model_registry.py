import pytest

from src.model_registry import ModelRegistry
from src.predict import load_release_models


@pytest.mark.parametrize(
    "record", load_release_models()["models"], ids=lambda r: r["name"]
)
def test_full_pipeline_predicts_and_is_cached(record):
    registry = ModelRegistry()
    args = dict(
        version=record["version"],
        artifact_path=record["artifact_path"],
        artifact_sha256=record["artifact_sha256"],
        artifact_format=record["format"],
    )
    predictor = registry.get_predictor(**args)
    result = predictor.predict(
        dict(
            pregnancies=0,
            glucose=0,
            blood_pressure=0,
            skin_thickness=0,
            insulin=0,
            bmi=0,
            diabetes_pedigree_function=0.5,
            age=40,
        )
    )
    assert result["prediction"] in (0, 1)
    assert 0 <= result["probability"] <= 1
    assert registry.get_predictor(**args) is predictor


def test_checksum_mismatch_prevents_loading():
    record = load_release_models()["models"][0]
    with pytest.raises(ValueError):
        ModelRegistry().get_predictor(
            version=record["version"],
            artifact_path=record["artifact_path"],
            artifact_sha256="0" * 64,
        )
