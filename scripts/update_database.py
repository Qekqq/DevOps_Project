"""Применяет защиту обратной связи к существующей БД без удаления данных."""

from pathlib import Path

from src.db.database import get_engine


def main():
    source = Path(__file__).resolve().parents[1] / "db" / "03_audit.sql"
    sql = source.read_text(encoding="utf-8")
    marker = "CREATE FUNCTION protect_feedback_identity()"
    if sql.count(marker) != 1:
        raise RuntimeError("Не найдено однозначное определение защиты обратной связи")
    definition = marker + sql.split(marker, 1)[1]
    definition = definition.replace(
        marker, "CREATE OR REPLACE FUNCTION protect_feedback_identity()", 1
    )
    definition = definition.replace(
        "CREATE TRIGGER feedback_identity_guard",
        "DROP TRIGGER IF EXISTS feedback_identity_guard ON feedback;\n"
        "CREATE TRIGGER feedback_identity_guard",
        1,
    )
    with get_engine().begin() as connection:
        connection.exec_driver_sql("SET LOCAL lock_timeout = '10s'")
        connection.exec_driver_sql("SELECT pg_advisory_xact_lock(20260908, 1)")
        connection.exec_driver_sql(definition)
    print("Защита обратной связи обновлена; существующие данные сохранены.")


if __name__ == "__main__":
    main()
