import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from src.features import FEATURE_COLUMNS
from src.feedback_dataset import (
    load_snapshot,
    read_confirmed_studies,
    save_snapshot,
    split_by_patient,
)


def test_snapshot_metadata_tampering_is_rejected(tmp_path):
    frame = pd.DataFrame(
        [
            {
                "study_id": 1,
                "patient_code": "PAT001",
                "study_date": "2026-09-08",
                **{key: 1 for key in FEATURE_COLUMNS},
                "outcome": 0,
            }
        ]
    )
    path = save_snapshot(frame, tmp_path)
    metadata_path = path.parent / "dataset.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["filters"]["date_from"] = "2020-01-01"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError):
        load_snapshot(path)
    with pytest.raises(ValueError):
        save_snapshot(frame, tmp_path)


def test_named_snapshots_preserve_names_and_identity(tmp_path):
    from src.db.load_raw_dataset import import_raw_dataset

    frame = pd.DataFrame(
        [
            {
                "study_id": 15,
                "patient_code": "PAT015",
                "study_date": "2026-09-08",
                **{key: 1 for key in FEATURE_COLUMNS},
                "outcome": 0,
            }
        ]
    )
    first = save_snapshot(frame, tmp_path, name="  Проверка сентября  ")
    again = save_snapshot(frame, tmp_path, name="Проверка сентября")
    second = save_snapshot(frame, tmp_path, name="Контрольный снимок")
    assert first == again
    assert first != second
    assert first.read_bytes() == second.read_bytes()
    assert load_snapshot(first)[1]["name"] == "Проверка сентября"
    assert load_snapshot(second)[1]["name"] == "Контрольный снимок"
    # При регистрации выпуска имя должно браться из самого снимка.
    db = Mock()
    db.execute.return_value.scalar_one_or_none.return_value = None
    dataset = import_raw_dataset(db, first)
    assert dataset.dataset_name == "Проверка сентября"
    assert db.add_all.call_args.args[0][0].source_study_id == 15
    metadata_path = first.parent / "dataset.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["name"] = "Подменённое название"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="Идентификатор снимка"):
        load_snapshot(first)


def test_snapshot_date_filter_includes_boundaries_and_negative_feedback(tmp_path):
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE studies (id INTEGER PRIMARY KEY, patient_code TEXT, "
                "study_date DATE, created_by INTEGER, created_at DATETIME, "
                + ", ".join(f"{name} NUMERIC" for name in FEATURE_COLUMNS)
                + ")"
            )
        )
        connection.execute(
            text("CREATE TABLE feedback (study_id INTEGER, true_label INTEGER)")
        )
        for number in range(1, 6):
            connection.execute(
                text(
                    "INSERT INTO studies VALUES (:id, 'PAT001', :day, NULL, NULL, 1,1,1,1,1,1,1,1)"
                ),
                {
                    "id": number,
                    "day": f"2026-09-0{number}",
                    "features": json.dumps({key: 1 for key in FEATURE_COLUMNS}),
                },
            )
        for number, label in [(1, 1), (2, 0), (4, 1), (5, 0)]:
            connection.execute(
                text("INSERT INTO feedback VALUES (:id, :label)"),
                {"id": number, "label": label},
            )
    with Session(engine) as db:
        frame = read_confirmed_studies(
            db,
            date_from=date(2026, 9, 2),
            date_to=date(2026, 9, 4),
        )
        assert frame["study_id"].tolist() == [2, 4]
        assert frame["outcome"].tolist() == [0, 1]
        assert len(read_confirmed_studies(db)) == 4
        with pytest.raises(ValueError):
            read_confirmed_studies(
                db,
                date_from=date(2026, 9, 4),
                date_to=date(2026, 9, 2),
            )
    path = save_snapshot(
        frame,
        tmp_path,
        date_from=date(2026, 9, 2),
        date_to=date(2026, 9, 4),
    )
    _, metadata = load_snapshot(path)
    assert metadata["filters"] == {"date_from": "2026-09-02", "date_to": "2026-09-04"}


def test_export_uses_one_label_per_study_without_prediction_join():
    study = SimpleNamespace(
        id=1,
        patient_code="PAT001",
        study_date=date(2026, 9, 8),
        features={key: 1 for key in FEATURE_COLUMNS},
    )
    db = Mock()
    db.execute.return_value.all.return_value = [(study, 0)]
    frame = read_confirmed_studies(db)
    assert len(frame) == 1
    assert frame.iloc[0]["outcome"] == 0
    assert "prediction_history" not in str(db.execute.call_args.args[0])


def test_patient_split_keeps_all_studies_of_a_patient_together(tmp_path):
    frame = pd.DataFrame(
        [
            {
                "study_id": patient * 2 + label,
                "patient_code": f"PAT{patient:03}",
                "outcome": label,
                "study_date": "2026-09-08",
                **{key: 1 for key in FEATURE_COLUMNS},
            }
            for patient in range(30)
            for label in (0, 1)
        ]
    )
    train, valid, test = split_by_patient(frame)
    groups = [set(part["patient_code"]) for part in (train, valid, test)]
    assert groups[0].isdisjoint(groups[1])
    assert groups[0].isdisjoint(groups[2])
    assert groups[1].isdisjoint(groups[2])
    assert sum(map(len, (train, valid, test))) == len(frame)
    path = save_snapshot(frame, tmp_path)
    assert save_snapshot(frame, tmp_path) == path
    assert list(pd.read_csv(path).columns) == [*FEATURE_COLUMNS, "outcome"]
    restored, metadata = load_snapshot(path)
    assert restored["patient_code"].tolist() == frame["patient_code"].tolist()
    assert metadata["rows"] == len(frame)
