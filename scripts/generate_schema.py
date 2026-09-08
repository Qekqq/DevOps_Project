"""Генерация SQL из ORM: один источник структуры и ограничений."""

import argparse
from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from src.db.models import Base


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    dialect = postgresql.dialect()
    statements = [
        "-- Создано командой python -m scripts.generate_schema; не редактируйте вручную."
    ]
    for table in Base.metadata.sorted_tables:
        statements.append(str(CreateTable(table).compile(dialect=dialect)) + ";")
        for index in sorted(table.indexes, key=lambda item: item.name):
            statements.append(str(CreateIndex(index).compile(dialect=dialect)) + ";")
    content = "\n\n".join(statements)
    content = "\n".join(line.rstrip() for line in content.splitlines()) + "\n"
    path = root / "db/01_schema.sql"
    if args.check:
        if path.read_text(encoding="utf-8") != content:
            raise SystemExit(
                "SQL-схема отличается от ORM. Выполните python -m scripts.generate_schema"
            )
    else:
        path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
