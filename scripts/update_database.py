"""Обновляет схему существующей БД при запуске новой версии приложения."""

import re
from pathlib import Path

from sqlalchemy import text

from src.db.database import get_engine
from src.db.models import StudyEdit, UserSession


def main():
    source = Path(__file__).resolve().parents[1] / "db" / "02_audit.sql"
    sql = source.read_text(encoding="utf-8")
    # Обновляем все функции из того же файла, который используется при создании БД.
    # Повторный запуск не меняет строки и не создаёт дублирующие триггеры.
    definition = sql.replace("CREATE FUNCTION ", "CREATE OR REPLACE FUNCTION ")
    definition = re.sub(
        r"CREATE TRIGGER (\w+) ([^;]*? ON (\w+)\s+)",
        r"DROP TRIGGER IF EXISTS \1 ON \3;\nCREATE TRIGGER \1 \2",
        definition,
    )
    with get_engine().begin() as connection:
        connection.exec_driver_sql("SET LOCAL lock_timeout = '10s'")
        connection.exec_driver_sql("SELECT pg_advisory_xact_lock(20260908, 1)")
        UserSession.__table__.create(connection, checkfirst=True)
        StudyEdit.__table__.create(connection, checkfirst=True)
        connection.exec_driver_sql(
            "ALTER TABLE predictions DROP COLUMN IF EXISTS request_source"
        )
        connection.execute(text(definition))
    print("Схема и функции аудита обновлены; существующие данные сохранены.")


if __name__ == "__main__":
    main()
