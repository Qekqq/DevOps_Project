"""Активация зарегистрированного выпуска с сохранением прежних версий в архиве."""

import argparse

from sqlalchemy import select, text

from src.db.database import get_session_factory
from src.db.models import ModelVersion
from src.register_release import validate_release


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest")
    args = parser.parse_args()
    manifest = validate_release(args.manifest)
    versions = {record["version"]: record for record in manifest["models"]}
    with get_session_factory()() as db:
        db.execute(text("LOCK TABLE model_versions IN EXCLUSIVE MODE"))
        records = db.scalars(select(ModelVersion)).all()
        registered = {record.model_version: record for record in records}
        for version, spec in versions.items():
            record = registered.get(version)
            if record is None or record.artifact_sha256 != spec["artifact_sha256"]:
                raise ValueError("Выпуск не зарегистрирован или контрольная сумма отличается")
            if record.artifact_path != spec["artifact_path"] or record.artifact_format != spec["format"]:
                raise ValueError("Путь или формат зарегистрированного артефакта отличается")
        for record in records:
            record.role = "archived"
            record.traffic_weight = 0
        db.flush()
        for version in versions:
            registered[version].role = "challenger"
        champion = registered[manifest["champion_version"]]
        champion.role = "champion"
        champion.traffic_weight = 100
        db.commit()
    print("Выпуск активирован; предыдущие версии сохранены в архиве.")


if __name__ == "__main__":
    main()
