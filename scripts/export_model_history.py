"""Ретроспективные месячные показатели по датам исследования, только чтение БД.

CSV содержит агрегаты, не персональные строки. Это восстановление по данным,
известным сейчас, а не утверждение, что модели работали в указанные даты.
"""

import argparse
import csv
import sys
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select, text

from src.db.database import get_session_factory
from src.db.models import ModelVersion
from src.features import ZERO_AS_MISSING_COLUMNS
from src.model_health import read_model_health


def history_rows(db, *, days=60, end=None):
    if not 1 <= days <= 366:
        raise ValueError("Период истории должен быть от 1 до 366 дней")
    end = end or datetime.now(timezone.utc).date()
    models = list(
        db.scalars(
            select(ModelVersion.id)
            .where(ModelVersion.role.in_(["champion", "challenger"]))
            .order_by(ModelVersion.id)
        )
    )
    for offset in range(days - 1, -1, -1):
        day = end - timedelta(days=offset)
        for model_id in models:
            snapshot = read_model_health(db, day, model_id)
            reference = snapshot["reference"]
            if reference is None:
                continue
            common = {
                "date": day.isoformat(),
                "version": reference["version"],
                "role": reference["role"],
                "dataset_id": reference["dataset_id"],
            }
            for feature in ZERO_AS_MISSING_COLUMNS:
                yield {
                    **common,
                    "metric": "missing_measurements",
                    "feature": feature,
                    "value": snapshot["quality"][feature + "_zeros"],
                    "samples": snapshot["quality"]["samples"],
                }
            for feature, value in snapshot["drift"].items():
                yield {
                    **common,
                    "metric": "data_drift_psi",
                    "feature": feature,
                    "value": value,
                    "samples": snapshot["current_samples"],
                }
            yield {
                **common,
                "metric": "target_drift",
                "value": snapshot["target_shift"],
                "samples": snapshot["labeled"],
            }
            for name, value in snapshot["classification"].items():
                yield {
                    **common,
                    "metric": name,
                    "value": value,
                    "samples": snapshot["evaluated"],
                }
            for name, value in snapshot["confusion"].items():
                yield {
                    **common,
                    "metric": "confusion_" + name,
                    "value": value,
                    "samples": snapshot["evaluated"],
                }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--end", type=date.fromisoformat)
    args = parser.parse_args()
    writer = csv.DictWriter(
        sys.stdout,
        fieldnames=[
            "date",
            "version",
            "role",
            "dataset_id",
            "metric",
            "feature",
            "value",
            "samples",
        ],
    )
    writer.writeheader()
    with get_session_factory()() as db:
        db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        db.execute(text("SET LOCAL statement_timeout = '5s'"))
        writer.writerows(history_rows(db, days=args.days, end=args.end))


if __name__ == "__main__":
    main()
