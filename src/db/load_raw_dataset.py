"""Загрузка неизменяемой raw-версии: повторная загрузка не создаёт дубли."""

import argparse
import json
from hashlib import sha256
from pathlib import Path

import pandas as pd
from sqlalchemy import select, text

from src.config import get_path, load_config
from src.datasets import read_raw_dataset
from src.db.database import get_session_factory
from src.db.models import Dataset, RawDatasetSample


def import_raw_dataset(db, path, name="pima_diabetes"):
    frame, audit = read_raw_dataset(Path(path))
    digest = audit["sha256"]
    snapshot = None
    source = None
    version = "raw-" + digest[:24]
    if (Path(path).parent / "dataset.json").exists():
        from src.feedback_dataset import load_snapshot

        source, snapshot = load_snapshot(path)
        version = sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": "dataset:" + digest},
    )
    existing = db.execute(
        select(Dataset).where(Dataset.dataset_version == version)
    ).scalar_one_or_none()
    if existing is not None:
        if existing.row_count != len(frame):
            raise ValueError(
                "Количество строк зарегистрированного датасета не совпадает"
            )
        return existing
    dataset = Dataset(
        dataset_name=snapshot.get("name", name) if snapshot else name,
        dataset_version=version,
        source_path=Path(path).as_posix(),
        source_sha256=digest,
        row_count=len(frame),
        source_type="feedback" if snapshot else "raw",
        selection_filters=snapshot.get("filters", {}) if snapshot else {},
        lineage_sha256=snapshot["lineage_sha256"] if snapshot else None,
    )
    db.add(dataset)
    db.flush()
    # JSONB хранит SQL-совместимый null, а не нестандартный JSON NaN.
    rows = frame.astype(object).where(pd.notna(frame), None).to_dict(orient="records")
    db.add_all(
        [
            RawDatasetSample(
                dataset_id=dataset.id,
                row_number=number,
                sample_values=values,
                source_study_id=int(source.iloc[number]["study_id"])
                if source is not None
                else None,
            )
            for number, values in enumerate(rows)
        ]
    )
    db.flush()
    return dataset


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    path = get_path(load_config(), "paths", "raw_data_path")
    _, audit = read_raw_dataset(path)
    print(audit)
    if args.apply:
        with get_session_factory()() as db:
            import_raw_dataset(db, path)
            db.commit()
        print("Исходный датасет зарегистрирован")


if __name__ == "__main__":
    run()
