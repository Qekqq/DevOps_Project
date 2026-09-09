"""Снимки подтверждённых исследований и разбиение по пациентам."""

import json
import re
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd
from sklearn.model_selection import GroupShuffleSplit
from sqlalchemy import select

from src.datasets import read_raw_dataset
from src.db.models import PredictionFeedback, Study
from src.features import FEATURE_COLUMNS


def validate_snapshot_name(value):
    if not isinstance(value, str):
        raise ValueError("Введите название снимка")
    value = value.strip()
    if not 1 <= len(value) <= 100:
        raise ValueError("Название снимка должно содержать от 1 до 100 символов")
    if re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]', value):
        raise ValueError(
            'Название не должно содержать символы <>:"/\\|?* и переносы строк'
        )
    return value


def snapshot_identity(content, lineage, filters, name=None):
    # Старые безымянные снимки сохраняют прежние идентификаторы.
    identity = dict(filters)
    if name is not None:
        identity["name"] = validate_snapshot_name(name)
    return sha256(
        content + lineage + json.dumps(identity, sort_keys=True).encode("utf-8")
    ).hexdigest()


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
    return pd.DataFrame(
        [
            {
                "study_id": study.id,
                "patient_code": study.patient_code,
                "study_date": study.study_date.isoformat(),
                **{key: study.features[key] for key in FEATURE_COLUMNS},
                "outcome": label,
            }
            for study, label in rows
        ],
        columns=["study_id", "patient_code", "study_date", *FEATURE_COLUMNS, "outcome"],
    )


def save_snapshot(frame, directory, *, date_from=None, date_to=None, name=None):
    if name is not None:
        name = validate_snapshot_name(name)
    if date_from is not None and date_to is not None and date_from > date_to:
        raise ValueError("Начало периода не может быть позже окончания")
    if frame.empty:
        raise ValueError("Нет исследований с подтверждённой обратной связью")
    if frame["study_id"].duplicated().any():
        raise ValueError("В снимке повторяется идентификатор исследования")
    if not frame["outcome"].isin([0, 1]).all():
        raise ValueError("Обратная связь должна содержать 0 или 1")
    content = (
        frame.loc[:, [*FEATURE_COLUMNS, "outcome"]]
        .to_csv(
            index=False,
            lineterminator="\n",
        )
        .encode("utf-8")
    )
    lineage = (
        frame.loc[:, ["study_id", "patient_code", "study_date"]]
        .to_json(
            orient="records",
            force_ascii=False,
        )
        .encode("utf-8")
    )
    digest = sha256(content).hexdigest()
    filters = {
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
    }
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    folder = directory / snapshot_identity(content, lineage, filters, name)
    path = folder / "data.csv"
    if folder.exists():
        load_snapshot(path)
        if (
            path.read_bytes() != content
            or (folder / "lineage.json").read_bytes() != lineage
        ):
            raise ValueError("Существующий снимок не совпадает с контрольной суммой")
        return path
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
    if name is not None:
        metadata["name"] = name
    # Публикуем каталог целиком: сбой записи не оставляет видимый неполный снимок.
    with TemporaryDirectory(prefix=".snapshot-", dir=directory) as temporary:
        staging = Path(temporary) / "snapshot"
        staging.mkdir()
        (staging / "data.csv").write_bytes(content)
        (staging / "lineage.json").write_bytes(lineage)
        (staging / "dataset.json").write_text(
            json.dumps(metadata, indent=2), encoding="utf-8"
        )
        try:
            staging.rename(folder)
        except OSError:
            if not folder.exists():
                raise
            # Другой процесс мог одновременно закончить тот же снимок.
            load_snapshot(path)
    return path


def load_snapshot(path):
    path = Path(path)
    frame, audit = read_raw_dataset(path)
    metadata = json.loads((path.parent / "dataset.json").read_text(encoding="utf-8"))
    lineage_bytes = (path.parent / "lineage.json").read_bytes()
    if (
        audit["sha256"] != metadata["sha256"]
        or sha256(lineage_bytes).hexdigest() != metadata["lineage_sha256"]
    ):
        raise ValueError("Снимок или сведения о происхождении были изменены")
    lineage = pd.DataFrame(json.loads(lineage_bytes))
    if (
        set(lineage.columns) != {"study_id", "patient_code", "study_date"}
        or lineage.isna().any().any()
    ):
        raise ValueError("Некорректные сведения о происхождении снимка")
    if (
        len(lineage) != len(frame)
        or metadata["rows"] != len(frame)
        or lineage["study_id"].duplicated().any()
    ):
        raise ValueError("Нарушено соответствие строк снимка исследованиям")
    if (
        metadata["patients"] != lineage["patient_code"].nunique()
        or metadata["target"] != "outcome"
    ):
        raise ValueError("Метаданные снимка не соответствуют его строкам")
    expected = snapshot_identity(
        path.read_bytes(), lineage_bytes, metadata["filters"], metadata.get("name")
    )
    if path.parent.name != expected:
        raise ValueError("Идентификатор снимка не соответствует данным и фильтру")
    return pd.concat(
        [lineage.reset_index(drop=True), frame.reset_index(drop=True)], axis=1
    ), metadata


def split_by_patient(frame, *, random_state=57, valid_size=0.15, test_size=0.15):
    if not 0 < valid_size < 1 or not 0 < test_size < 1 or valid_size + test_size >= 1:
        raise ValueError("Некорректные доли validation и test")
    if frame["patient_code"].isna().any() or frame["patient_code"].nunique() < 3:
        raise ValueError("Для разбиения нужны как минимум три разных пациента")
    first = GroupShuffleSplit(
        n_splits=1, test_size=test_size, random_state=random_state
    )
    train_valid_idx, test_idx = next(first.split(frame, groups=frame["patient_code"]))
    train_valid = frame.iloc[train_valid_idx]
    second = GroupShuffleSplit(
        n_splits=1,
        test_size=valid_size / (1 - test_size),
        random_state=random_state,
    )
    train_idx, valid_idx = next(
        second.split(train_valid, groups=train_valid["patient_code"])
    )
    parts = (
        train_valid.iloc[train_idx],
        train_valid.iloc[valid_idx],
        frame.iloc[test_idx],
    )
    if any(set(part["outcome"]) != {0, 1} for part in parts):
        raise ValueError(
            "В каждой выборке нужны оба класса; накопите больше подтверждённых данных"
        )
    return parts
