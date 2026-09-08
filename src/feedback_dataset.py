"""Снимки подтверждённых исследований и разбиение по пациентам."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd
from sklearn.model_selection import GroupShuffleSplit
from sqlalchemy import select

from src.db.models import PredictionFeedback, Study
from src.features import FEATURE_COLUMNS
from src.datasets import read_raw_dataset


def read_confirmed_studies(db, *, date_from=None, date_to=None):
    # Не соединяем с predictions: число моделей не влияет на число строк.
    if date_from is not None and date_to is not None and date_from > date_to:
        raise ValueError("Начало периода не может быть позже окончания")
    query = (
        select(Study, PredictionFeedback.true_label)
        .join(PredictionFeedback, PredictionFeedback.study_id == Study.id)
        .order_by(Study.id)
    ).where(PredictionFeedback.true_label.is_not(None))
    if date_from is not None:
        query = query.where(Study.study_date >= date_from)
    if date_to is not None:
        query = query.where(Study.study_date <= date_to)
    rows = db.execute(query).all()
    return pd.DataFrame([
        {
            "study_id": study.id,
            "patient_code": study.patient_code,
            "study_date": study.study_date.isoformat(),
            **{key: study.features[key] for key in FEATURE_COLUMNS},
            "outcome": label,
        }
        for study, label in rows
    ], columns=["study_id", "patient_code", "study_date", *FEATURE_COLUMNS, "outcome"])


def save_snapshot(frame, directory, *, date_from=None, date_to=None):
    if date_from is not None and date_to is not None and date_from > date_to:
        raise ValueError("Начало периода не может быть позже окончания")
    if frame.empty:
        raise ValueError("Нет исследований с подтверждённой обратной связью")
    if frame["study_id"].duplicated().any():
        raise ValueError("В снимке повторяется идентификатор исследования")
    if not frame["outcome"].isin([0, 1]).all():
        raise ValueError("Обратная связь должна содержать 0 или 1")
    content = frame.loc[:, [*FEATURE_COLUMNS, "outcome"]].to_csv(
        index=False, lineterminator="\n",
    ).encode("utf-8")
    lineage = frame.loc[:, ["study_id", "patient_code", "study_date"]].to_json(
        orient="records", force_ascii=False,
    ).encode("utf-8")
    digest = sha256(content).hexdigest()
    filters = {
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
    }
    filter_bytes = json.dumps(filters, sort_keys=True).encode("utf-8")
    folder = Path(directory) / sha256(content + lineage + filter_bytes).hexdigest()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "data.csv"
    if path.exists():
        if path.read_bytes() != content or (folder / "lineage.json").read_bytes() != lineage:
            raise ValueError("Существующий снимок не совпадает с контрольной суммой")
        return path
    with path.open("xb") as file:
        file.write(content)
    (folder / "lineage.json").write_bytes(lineage)
    metadata = {
        "sha256": digest,
        "lineage_sha256": sha256(lineage).hexdigest(),
        "rows": len(frame),
        "patients": int(frame["patient_code"].nunique()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": "studies JOIN feedback",
        "target": "outcome",
        "filters": filters,
    }
    (folder / "dataset.json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8",
    )
    return path


def load_snapshot(path):
    path = Path(path)
    frame, audit = read_raw_dataset(path)
    metadata = json.loads((path.parent / "dataset.json").read_text(encoding="utf-8"))
    lineage_bytes = (path.parent / "lineage.json").read_bytes()
    if audit["sha256"] != metadata["sha256"] or sha256(lineage_bytes).hexdigest() != metadata["lineage_sha256"]:
        raise ValueError("Снимок или сведения о происхождении были изменены")
    lineage = pd.DataFrame(json.loads(lineage_bytes))
    if len(lineage) != len(frame) or metadata["rows"] != len(frame) or lineage["study_id"].duplicated().any():
        raise ValueError("Нарушено соответствие строк снимка исследованиям")
    return pd.concat([lineage.reset_index(drop=True), frame.reset_index(drop=True)], axis=1), metadata


def split_by_patient(frame, *, random_state=57, valid_size=0.15, test_size=0.15):
    if not 0 < valid_size < 1 or not 0 < test_size < 1 or valid_size + test_size >= 1:
        raise ValueError("Некорректные доли validation и test")
    if frame["patient_code"].isna().any() or frame["patient_code"].nunique() < 3:
        raise ValueError("Для разбиения нужны как минимум три разных пациента")
    first = GroupShuffleSplit(n_splits=1, test_size=test_size, random_state=random_state)
    train_valid_idx, test_idx = next(first.split(frame, groups=frame["patient_code"]))
    train_valid = frame.iloc[train_valid_idx]
    second = GroupShuffleSplit(
        n_splits=1, test_size=valid_size / (1 - test_size), random_state=random_state,
    )
    train_idx, valid_idx = next(second.split(train_valid, groups=train_valid["patient_code"]))
    parts = (train_valid.iloc[train_idx], train_valid.iloc[valid_idx], frame.iloc[test_idx])
    if any(set(part["outcome"]) != {0, 1} for part in parts):
        raise ValueError("В каждой выборке нужны оба класса; накопите больше подтверждённых данных")
    return parts
