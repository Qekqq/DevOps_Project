"""Резервная копия PostgreSQL без вывода паролей и данных в терминал."""

import subprocess
from datetime import datetime, timezone
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    folder = root / "backups"
    folder.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = folder / f"postgres-{stamp}.dump"
    command = [
        "docker",
        "compose",
        "exec",
        "-T",
        "db",
        "sh",
        "-c",
        'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc',
    ]
    with path.open("xb") as output:
        result = subprocess.run(command, cwd=root, stdout=output, check=False)
    if result.returncode:
        raise SystemExit(f"Ошибка копирования; неполный файл не использовать: {path}")
    with path.open("rb") as source:
        subprocess.run(
            ["docker", "compose", "exec", "-T", "db", "pg_restore", "--list"],
            cwd=root,
            stdin=source,
            stdout=subprocess.DEVNULL,
            check=True,
        )
    print(f"Архив создан, оглавление проверено: {path}")
    print("Это проверка формата архива; полное восстановление проверяется отдельно.")


if __name__ == "__main__":
    main()
