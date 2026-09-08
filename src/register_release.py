"""Проверка выпуска; запись в БД выполняется только с явным --apply."""
import argparse
from hashlib import sha256
import json
from pathlib import Path

from sqlalchemy import select

from src.config import get_project_root
from src.db.database import get_session_factory
from src.db.models import ModelVersion
from src.model_registry import ModelRegistry
from src.db.load_raw_dataset import import_raw_dataset
from src.feedback_dataset import load_snapshot


def validate_release(path):
    root = get_project_root().resolve()
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    dataset = manifest["dataset"]
    dataset_path = (root / dataset["path"]).resolve()
    if not dataset_path.is_relative_to(root) or sha256(dataset_path.read_bytes()).hexdigest() != dataset["sha256"]:
        raise ValueError("Исходный датасет не совпадает с выпуском")
    if dataset.get("snapshot_metadata") is not None:
        _, snapshot_metadata = load_snapshot(dataset_path)
        if snapshot_metadata != dataset["snapshot_metadata"]:
            raise ValueError("Сведения о снимке отличаются от записанных при обучении")
    row_ids = [row for split in ("train", "validation", "test") for row in dataset["row_ids"][split]]
    if sorted(row_ids) != list(range(dataset["rows"])):
        raise ValueError("Разбиение содержит пропущенные или повторяющиеся строки")
    versions = [record["version"] for record in manifest["models"]]
    if len(set(versions)) != len(versions) or manifest["champion_version"] not in versions:
        raise ValueError("Некорректные версии или champion в манифесте")
    registry = ModelRegistry()
    for record in manifest["models"]:
        if not 1 <= len(record["version"]) <= 50:
            raise ValueError("Версия должна содержать от 1 до 50 символов")
        if record["format"] != "full-pipeline-v1":
            raise ValueError("Ожидается полный pipeline")
        artifact = (root / record["artifact_path"]).resolve()
        if not artifact.is_relative_to(root) or sha256(artifact.read_bytes()).hexdigest() != record["artifact_sha256"]:
            raise ValueError("Неверный путь или контрольная сумма артефакта")
        predictor = registry.get_predictor(version=record["version"], artifact_path=record["artifact_path"],
            artifact_sha256=record["artifact_sha256"], artifact_format=record["format"])
        predictor.predict(dict(pregnancies=0, glucose=0, blood_pressure=0, skin_thickness=0,
                               insulin=0, bmi=0, diabetes_pedigree_function=.5, age=40))
    return manifest


def register_release(manifest, db, *, dataset_id=None):
    """Регистрирует challenger; существующие роли и артефакты не перезаписывает."""
    for record in manifest["models"]:
        existing = db.execute(select(ModelVersion).where(ModelVersion.model_version == record["version"])).scalar_one_or_none()
        if existing is not None:
            if existing.artifact_sha256 != record["artifact_sha256"] or existing.artifact_path != record["artifact_path"] or existing.artifact_format != record["format"]:
                raise ValueError("Версия уже зарегистрирована с другим артефактом")
            continue
        metrics = record["validation"]
        db.add(ModelVersion(
            model_name=record["name"], model_version=record["version"],
            artifact_path=record["artifact_path"], artifact_sha256=record["artifact_sha256"],
            artifact_format=record["format"], preprocessing_version=record["format"],
            train_medians=None, params_json=record["parameters"],
            accuracy_score=metrics["accuracy"], precision_score=metrics["precision"],
            recall_score=metrics["recall"], f1_score=metrics["f1"],
            traffic_weight=0, role="challenger",
            trained_on_dataset_id=dataset_id,
            metadata_json={"release": manifest["release"], "dataset": manifest["dataset"],
                "provenance": manifest["provenance"], "test": record.get("test"),
                "recommended_champion": record["version"] == manifest["champion_version"]},
        ))
    db.flush()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    manifest = validate_release(args.manifest)
    if args.apply:
        with get_session_factory()() as db:
            dataset = import_raw_dataset(db, get_project_root() / manifest["dataset"]["path"])
            register_release(manifest, db, dataset_id=dataset.id)
            db.commit()
        print("Модели зарегистрированы как challenger")
    else:
        print("Выпуск проверен; БД не изменялась")
