import joblib
import pandas as pd
import numpy as np
import pytest
from sklearn.model_selection import GridSearchCV

from src.pipelines import build_pipeline
from src.features import FEATURE_COLUMNS


def test_pipeline_uses_training_statistics_and_survives_serialization(tmp_path):
    train = pd.DataFrame([
        [0, 100, 70, 20, 100, 30, .2, 25],
        [1, 120, 80, 40, 200, 40, .3, 35],
        [2, 0, 0, 0, 0, 0, .4, 45],
        [3, 140, 90, 30, 300, 35, .5, 55],
    ], columns=FEATURE_COLUMNS)
    pipeline = build_pipeline("decision_tree", random_state=57, parameters={"max_depth": 2})
    pipeline.fit(train, [0, 0, 1, 1])
    before = pipeline.named_steps["imputer"].statistics_.copy()
    request = train.iloc[[2]].copy()
    request["pregnancies"] = 0
    transformed = pipeline[:-1].transform(request)
    assert transformed[0, 0] == 0
    assert transformed[0, 1] == 120
    assert transformed[0, 4] == 200
    extreme = request.copy()
    extreme["glucose"] = 100000
    pipeline.predict(extreme)
    np.testing.assert_array_equal(before, pipeline.named_steps["imputer"].statistics_)
    path = tmp_path / "model.joblib"
    joblib.dump(pipeline, path)
    restored = joblib.load(path)
    np.testing.assert_array_equal(pipeline.predict(request), restored.predict(request))
    pd.testing.assert_frame_equal(request, train.iloc[[2]].assign(pregnancies=0))


def test_mixed_imputation_keeps_training_statistics_order_and_serialization(tmp_path):
    train = pd.DataFrame([
        [0, 100, 70, 20, 100, 30, .2, 25],
        [1, 110, 80, 30, 200, 35, .3, 35],
        [2, 210, 150, 90, 600, 80, .4, 45],
        [3, 0, 0, 0, np.nan, 0, .5, 55],
    ], columns=FEATURE_COLUMNS)
    pipeline = build_pipeline(
        "decision_tree", random_state=57, parameters={"max_depth": 2},
        preprocessing={
            "imputation": "median", "scale": False,
            "imputation_by_feature": {"insulin": "mean", "glucose": "mean"},
        },
    )
    pipeline.fit(train, [0, 0, 1, 1])
    request = train.iloc[[3]].assign(pregnancies=0)
    expected = [[0, 140, 80, 30, 300, 35, .5, 55]]
    np.testing.assert_allclose(pipeline[:-1].transform(request), expected)
    np.testing.assert_allclose(
        pipeline[:-1].transform(request[FEATURE_COLUMNS[::-1]]), expected,
    )
    pipeline.predict(train.assign(glucose=500))
    np.testing.assert_allclose(pipeline[:-1].transform(request), expected)
    path = tmp_path / "mixed.joblib"
    joblib.dump(pipeline, path)
    restored = joblib.load(path)
    np.testing.assert_allclose(restored[:-1].transform(request), expected)
    search = GridSearchCV(
        pipeline, {"imputer__glucose__strategy": ["median", "mean"]}, cv=2,
        error_score="raise",
    ).fit(train, [0, 0, 1, 1])
    assert search.best_params_["imputer__glucose__strategy"] in {"median", "mean"}


@pytest.mark.parametrize("overrides", [{"typo": "mean"}, {"glucose": "unknown"}, []])
def test_mixed_imputation_rejects_invalid_configuration(overrides):
    with pytest.raises(ValueError):
        build_pipeline(
            "decision_tree", random_state=57, parameters={},
            preprocessing={"imputation_by_feature": overrides},
        )
