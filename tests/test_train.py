import json
import shutil
from pathlib import Path

import joblib
import pandas as pd
import pytest

from src import train as training
from src.data_preprocessing import DataPreprocessor
from src.features import FEATURE_COLUMNS
from src.feedback_dataset import save_snapshot
from src.pipelines import build_pipeline
from src.train import fit_candidate, metrics


@pytest.mark.parametrize(
    "settings",
    [
        {"models": {}},
        {"models": {"../escape": {"family": "decision_tree"}}},
        {"models": {"tree": {"family": "decision_tree", "serach": {}}}},
        {
            "models": {
                "tree": {
                    "family": "decision_tree",
                    "search": {"param_grid": {"classifier__typo": [1]}},
                }
            }
        },
    ],
)
def test_invalid_training_plan_does_not_create_release(tmp_path, monkeypatch, settings):
    (tmp_path / "training.json").write_text(json.dumps(settings), encoding="utf-8")
    monkeypatch.setattr(training, "get_project_root", lambda: tmp_path)
    with pytest.raises(ValueError):
        training.train_release()
    assert not (tmp_path / "models").exists()


def test_failed_manifest_publication_preserves_previous_release(tmp_path, monkeypatch):
    path = tmp_path / "current.json"
    path.write_text('{"release": "previous"}', encoding="utf-8")

    def fail_replace(*args):
        raise OSError("disk error")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError):
        training.publish_manifest(path, {"release": "new"})
    assert json.loads(path.read_text()) == {"release": "previous"}
    assert list(tmp_path.iterdir()) == [path]


def test_training_full_pipeline_returns_validation_metrics():
    processor = DataPreprocessor()
    X_train, X_valid, _, y_train, y_valid, _ = processor.split_data(
        processor.load_data()
    )
    pipeline = build_pipeline(
        "decision_tree", random_state=57, parameters={"max_depth": 4}
    )
    pipeline.fit(X_train, y_train)
    result = metrics(pipeline, X_valid, y_valid)
    assert set(result) == {"accuracy", "precision", "recall", "f1"}
    assert all(0 <= value <= 1 for value in result.values())
    assert pipeline.named_steps["imputer"].statistics_[4] == 126


def test_tuned_candidate_searches_preprocessing_inside_pipeline():
    processor = DataPreprocessor()
    X_train, _, _, y_train, _, _ = processor.split_data(processor.load_data())
    specification = {
        "family": "decision_tree",
        "parameters": {"max_depth": 2},
        "preprocessing": {"imputation": "mean", "scale": False},
        "search": {
            "folds": 2,
            "param_grid": {"imputer__strategy": ["mean", "median"]},
        },
    }
    model, result = fit_candidate(specification, X_train, y_train, 57)
    assert result["method"] == "GridSearchCV"
    assert (
        model.named_steps["imputer"].strategy
        == result["best_params"]["imputer__strategy"]
    )
    assert 0 <= result["best_cv_f1"] <= 1


def test_feedback_release_trains_without_identity_and_preserves_groups(
    tmp_path, monkeypatch
):
    root = Path(__file__).resolve().parents[1]
    for name in [
        "src/pipelines.py",
        "src/train.py",
        "src/data_preprocessing.py",
        "src/clinical_features_v1.py",
        "src/feedback_dataset.py",
        "src/datasets.py",
        "src/features.py",
        "src/config.py",
        "config.ini",
        "requirements.txt",
    ]:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, target)
    specification = {
        "family": "decision_tree",
        "preprocessing": {"imputation": "median", "scale": False},
        "search": {"folds": 2, "param_grid": {"classifier__max_depth": [2, 3]}},
    }
    (tmp_path / "training.json").write_text(
        json.dumps({"models": {"tree": specification}}),
        encoding="utf-8",
    )
    frame = pd.DataFrame(
        [
            {
                "study_id": patient * 2 + label,
                "patient_code": f"PAT{patient:03}",
                "study_date": f"2026-09-0{label + 1}",
                **{key: 1 for key in FEATURE_COLUMNS},
                "glucose": 90 + 60 * label,
                "outcome": label,
            }
            for patient in range(30)
            for label in (0, 1)
        ]
    )
    path = save_snapshot(frame, tmp_path / "data" / "feedback")
    monkeypatch.setattr(training, "get_project_root", lambda: tmp_path)
    monkeypatch.setattr(training.subprocess, "check_output", lambda *a, **kw: "test")
    manifest = training.train_release(feedback_snapshot=path)
    assert manifest["dataset"]["split_strategy"] == "patient_groups"
    partitions = [
        set(frame.loc[indices, "patient_code"])
        for indices in manifest["dataset"]["row_ids"].values()
    ]
    assert all(
        partitions[i].isdisjoint(partitions[j])
        for i in range(3)
        for j in range(i + 1, 3)
    )
    model = joblib.load(tmp_path / manifest["models"][0]["artifact_path"])
    assert list(model.feature_names_in_) == FEATURE_COLUMNS
    assert len(model.predict(frame[FEATURE_COLUMNS])) == len(frame)
    assert manifest["models"][0]["search_result"]["method"] == "GridSearchCV"
