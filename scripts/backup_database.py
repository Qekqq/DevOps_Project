"""Резервная копия PostgreSQL без вывода паролей и данных в терминал."""

import subprocess
from datetime import datetime, timezone
from pathlib import Path


def create_database_backup(folder: Path, *, container: str | None = None) -> Path:
    """Публикует .dump только после успешного pg_dump и проверки оглавления."""
    folder = folder.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = folder / f"postgres-{stamp}.dump"
    partial = path.with_suffix(".dump.partial")
    root = Path(__file__).resolve().parents[1]
    prefix = (
        ["docker", "exec", "-i", container]
        if container
        else ["docker", "compose", "exec", "-T", "db"]
    )
    try:
        with partial.open("xb") as output:
            subprocess.run(
                [
                    *prefix,
                    "sh",
                    "-c",
                    'exec pg_dump --no-password -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc',
                ],
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=output,
                check=True,
            )
        if partial.stat().st_size == 0:
            raise RuntimeError("pg_dump создал пустой файл")
        with partial.open("rb") as source:
            subprocess.run(
                [*prefix, "pg_restore", "--list"],
                cwd=root,
                stdin=source,
                stdout=subprocess.DEVNULL,
                check=True,
            )
        partial.replace(path)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        raise RuntimeError(
            f"Резервная копия не создана; обновление запрещено. Неполный файл не использовать: {partial}"
        ) from error
    return path


def main():
    root = Path(__file__).resolve().parents[1]
    path = create_database_backup(root / "backups")
    print(f"Архив создан, оглавление проверено: {path}")
    print("Это проверка формата архива; полное восстановление проверяется отдельно.")


if __name__ == "__main__":
    main()
