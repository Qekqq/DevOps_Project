"""Проверка восстановления архива в отдельной временной базе PostgreSQL."""

import argparse
import subprocess
from pathlib import Path
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    archive = args.archive.resolve(strict=True)
    database = "restore_check_" + uuid4().hex
    prefix = ["docker", "compose", "exec", "-T", "db", "sh", "-c"]
    subprocess.run(
        [*prefix, 'createdb -U "$POSTGRES_USER" "$1"', "sh", database],
        cwd=root,
        check=True,
    )
    try:
        with archive.open("rb") as source:
            subprocess.run(
                [
                    *prefix,
                    'pg_restore -U "$POSTGRES_USER" -d "$1" --exit-on-error',
                    "sh",
                    database,
                ],
                cwd=root,
                stdin=source,
                check=True,
            )
        print("Архив успешно восстановлен в отдельную временную БД.")
    finally:
        # Удаляется только база со случайным именем, созданная этим запуском.
        subprocess.run(
            [*prefix, 'dropdb -U "$POSTGRES_USER" "$1"', "sh", database],
            cwd=root,
            check=True,
        )


if __name__ == "__main__":
    main()
