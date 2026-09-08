"""python -m src.train: новый выпуск без изменения прежних артефактов."""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
import platform
import subprocess
from uuid import uuid4

import joblib
from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score
from sklearn.model_selection import GridSearchCV, StratifiedGroupKFold, StratifiedKFold

from src.config import get_project_root
from src.data_preprocessing import DataPreprocessor
from src.features import FEATURE_COLUMNS
from src.feedback_dataset import load_snapshot, split_by_patient
from src.pipelines import build_pipeline


def metrics(model, X, y):
    predicted = model.predict(X)
    return {
        "accuracy": float(accuracy_score(y, predicted)),
        "precision": float(precision_score(y, predicted, zero_division=0)),
        "recall": float(recall_score(y, predicted, zero_division=0)),
        "f1": float(f1_score(y, predicted, zero_division=0)),
    }


def fit_candidate(specification, X_train, y_train, random_state, groups=None):
    model = build_pipeline(
        specification["family"],
        random_state=random_state,
        parameters=specification.get("parameters", {}),
        preprocessing=specification.get("preprocessing", {}),
    )
    search_settings = specification.get("search")
    if not search_settings:
        model.fit(X_train, y_train)
        return model, None
    folds = search_settings.get("folds", 5)
    if groups is not None and len(set(groups)) < folds:
        raise ValueError("Для кросс-валидации недостаточно разных пациентов")
    splitter = StratifiedKFold if groups is None else StratifiedGroupKFold
    search = GridSearchCV(
        model,
        param_grid=search_settings["param_grid"],
        scoring="f1",
        cv=splitter(
            n_splits=folds,
            shuffle=True,
            random_state=random_state,
        ),
        n_jobs=1,
        error_score="raise",
    )
    search.fit(X_train, y_train, groups=groups)
    return search.best_estimator_, {
        "method": "GridSearchCV",
        "best_params": search.best_params_,
        "best_cv_f1": float(search.best_score_),
    }


def train_release(feedback_snapshot=None):
    root = get_project_root()
    settings = json.loads((root / "training.json").read_text(encoding="utf-8"))
    data = DataPreprocessor()
    groups = None
    snapshot_metadata = None
    if feedback_snapshot is None:
        frame = data.load_data()
        X_train, X_valid, X_test, y_train, y_valid, y_test = data.split_data(frame)
    else:
        data.raw_data_path = (root / Path(feedback_snapshot)).resolve()
        if not data.raw_data_path.is_relative_to(root.resolve()):
            raise ValueError("Снимок должен находиться внутри проекта")
        frame, snapshot_metadata = load_snapshot(data.raw_data_path)
        train, valid, test = split_by_patient(
            frame, random_state=data.random_state,
            valid_size=data.valid_size, test_size=data.test_size,
        )
        X_train, X_valid, X_test = [part[FEATURE_COLUMNS] for part in (train, valid, test)]
        y_train, y_valid, y_test = [part["outcome"] for part in (train, valid, test)]
        groups = train["patient_code"]
    release = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    output = root / "models" / release
    output.mkdir(parents=True, exist_ok=False)
    source_files = ["src/pipelines.py", "src/train.py", "src/data_preprocessing.py",
                    "src/feedback_dataset.py", "src/datasets.py",
                    "src/features.py", "src/config.py", "config.ini", "training.json", "requirements.txt"]
    provenance = {
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
        "git_dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).strip()),
        "source_sha256": {},
        "python": platform.python_version(),
        "dependencies": {name: version(name) for name in ["scikit-learn", "numpy", "pandas", "joblib", "scipy"]},
    }
    for name in source_files:
        content = (root / name).read_bytes()
        target = output / "source" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        provenance["source_sha256"][name] = sha256(content).hexdigest()
    dataset = {
        "path": data.raw_data_path.relative_to(root).as_posix(),
        "sha256": sha256(data.raw_data_path.read_bytes()).hexdigest(),
        "rows": len(frame), "random_state": data.random_state,
        "split_strategy": "patient_groups" if groups is not None else "stratified_rows",
        "snapshot_metadata": snapshot_metadata,
        "row_ids": {"train": X_train.index.tolist(), "validation": X_valid.index.tolist(), "test": X_test.index.tolist()},
    }
    records = []
    for number, (name, specification) in enumerate(settings["models"].items(), start=1):
        model, search_result = fit_candidate(
            specification, X_train, y_train, data.random_state, groups=groups,
        )
        path = output / name / "model.joblib"
        path.parent.mkdir()
        joblib.dump(model, path)
        records.append({
            "name": name, "version": release + f"-m{number}",
            "artifact_path": path.relative_to(root).as_posix(),
            "artifact_sha256": sha256(path.read_bytes()).hexdigest(),
            "format": "full-pipeline-v1", "parameters": specification,
            "search_result": search_result,
            "validation": metrics(model, X_valid, y_valid),
        })
    # Выбор не использует test. При равенстве F1 сохраняется порядок конфигурации.
    champion = max(records, key=lambda record: record["validation"]["f1"])
    champion_model = joblib.load(root / champion["artifact_path"])
    champion["test"] = metrics(champion_model, X_test, y_test)
    manifest = {"release": release, "dataset": dataset, "provenance": provenance,
                "selection_metric": "validation.f1", "champion_version": champion["version"], "models": records}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    (root / "models" / "current.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    reports = root / "reports"
    reports.mkdir(exist_ok=True)
    comparison = {
        record["name"]: {
            "validation": record["validation"],
            "best_cv_f1": record["search_result"]["best_cv_f1"]
            if record["search_result"] else None,
        }
        for record in records
    }
    (reports / "metrics.json").write_text(
        json.dumps(comparison, indent=2), encoding="utf-8",
    )
    (reports / "selected_parameters.json").write_text(
        json.dumps({
            record["name"]: {
                "version": record["version"],
                "dataset_sha256": dataset["sha256"],
                "preprocessing": record["parameters"]["preprocessing"],
                "search_result": record["search_result"],
            }
            for record in records
        }, indent=2),
        encoding="utf-8",
    )
    print(f"Выпуск сохранён: {output}")
    print(f"Рекомендованная champion: {champion['name']}; F1 на validation: {champion['validation']['f1']:.6f}")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--feedback-snapshot", help="Путь к data.csv снимка обратной связи")
    args = parser.parse_args()
    train_release(feedback_snapshot=args.feedback_snapshot)
