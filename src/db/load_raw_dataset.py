"""Загрузка неизменяемой raw-версии: повторная загрузка не создаёт дубли."""
import argparse
from pathlib import Path

from sqlalchemy import select, text

from src.config import get_path, load_config
from src.datasets import read_raw_dataset
from src.db.database import get_session_factory
from src.db.models import Dataset, RawDatasetSample


def import_raw_dataset(db, path, name="pima_diabetes"):
    frame, audit = read_raw_dataset(Path(path))
    digest = audit["sha256"]
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": "dataset:" + digest})
    existing = db.execute(select(Dataset).where(Dataset.source_sha256 == digest)).scalar_one_or_none()
    if existing is not None:
        if existing.row_count != len(frame):
            raise ValueError("Количество строк зарегистрированного датасета не совпадает")
        return existing
    dataset = Dataset(dataset_name=name, dataset_version="raw-" + digest[:24],
                      source_path=Path(path).as_posix(), source_sha256=digest, row_count=len(frame))
    db.add(dataset)
    db.flush()
    db.add_all([RawDatasetSample(dataset_id=dataset.id, row_number=number, sample_values=values)
                for number, values in enumerate(frame.to_dict(orient="records"))])
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
            dataset = import_raw_dataset(db, path)
            db.commit()
        print("Исходный датасет зарегистрирован")


if __name__ == "__main__":
    run()
