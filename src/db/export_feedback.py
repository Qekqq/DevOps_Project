"""Выгрузка и регистрация неизменяемого снимка обратной связи в БД."""

import argparse
from datetime import date

from src.config import get_project_root
from src.db.database import get_session_factory
from src.db.load_raw_dataset import import_raw_dataset
from src.feedback_dataset import (
    read_confirmed_studies,
    save_snapshot,
    validate_snapshot_name,
)


def run(*, name, date_from=None, date_to=None):
    name = validate_snapshot_name(name)
    with get_session_factory()() as db:
        frame = read_confirmed_studies(db, date_from=date_from, date_to=date_to)
    path = save_snapshot(
        frame,
        get_project_root() / "data" / "feedback",
        date_from=date_from,
        date_to=date_to,
        name=name,
    )
    with get_session_factory()() as db:
        import_raw_dataset(db, path, name=name)
        db.commit()
    print(f"Снимок подтверждённых исследований сохранён: {path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Снимок исследований с обратной связью"
    )
    parser.add_argument(
        "--name", required=True, type=validate_snapshot_name, help="Название снимка"
    )
    parser.add_argument(
        "--date-from", type=date.fromisoformat, help="Начало периода, ГГГГ-ММ-ДД"
    )
    parser.add_argument(
        "--date-to",
        type=date.fromisoformat,
        help="Конец периода включительно, ГГГГ-ММ-ДД",
    )
    args = parser.parse_args()
    run(name=args.name, date_from=args.date_from, date_to=args.date_to)
