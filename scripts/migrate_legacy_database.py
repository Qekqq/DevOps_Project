"""Однократный перенос старой БД с единственным прогнозом без даты."""

import argparse
from datetime import date
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--study-date", type=date.fromisoformat, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sql = [
        "BEGIN;",
        "LOCK TABLE prediction_history, prediction_feedback IN ACCESS EXCLUSIVE MODE;",
        "DO $$ BEGIN IF (SELECT count(*) FROM prediction_history) <> 1 THEN "
        "RAISE EXCEPTION 'Expected exactly one legacy prediction'; END IF; END $$;",
    ]
    migrations = root / "db" / "migrations"
    for name in [
        "002_prediction_study.sql", "003_studies_and_feedback.sql",
        "004_model_artifacts.sql", "005_full_pipelines.sql", "006_raw_datasets.sql",
    ]:
        content = (migrations / name).read_text(encoding="utf-8")
        sql.append("\n".join(
            line for line in content.splitlines()
            if line.strip() not in {"BEGIN;", "COMMIT;"}
        ))
        if name.startswith("002"):
            sql.append(
                "UPDATE prediction_history SET study_date = DATE "
                f"'{args.study_date.isoformat()}' WHERE study_date IS NULL;"
            )
    sql.append("COMMIT;")
    subprocess.run(
        ["docker", "compose", "exec", "-T", "db", "sh", "-c",
         'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1'],
        cwd=root, input="\n".join(sql).encode("utf-8"), check=True,
    )
    print("Миграция завершена одной транзакцией. История сохранена.")


if __name__ == "__main__":
    main()
