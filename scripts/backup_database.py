"""Резервная копия PostgreSQL без вывода паролей и данных в терминал."""

import argparse
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def prune_release_backups(home: Path, *, keep: int = 2) -> int:
    """Prune completed dumps after successful CD; preserve migration recovery.

    No recursive deletion: only files with the names emitted by pg_dump helper
    and a valid archive signature, directly inside this installation's backups.
    """
    if keep < 2:
        raise ValueError("Keep at least two completed backups")
    home = home.resolve(strict=True)
    folder = home / "backups"
    if folder.is_symlink() or (folder.exists() and folder.resolve() != folder):
        raise ValueError("Backup directory must not be a link")
    if not folder.exists() or (home / "security-migration.pending.json").exists():
        return 0
    protected = set()
    for journal in (home / "security-migrations").glob("*/journal.json"):
        state = json.loads(journal.read_text(encoding="utf-8"))
        if state.get("database_backup"):
            protected.add(Path(state["database_backup"]).resolve())
    state_file = home / "current.json"
    if state_file.exists():
        state = json.loads(state_file.read_text(encoding="utf-8"))
        if state.get("database_backup"):
            protected.add(Path(state["database_backup"]).resolve())
    candidates = []
    for path in folder.iterdir():
        if not re.fullmatch(r"postgres-\d{8}T\d{12}Z\.dump", path.name):
            continue
        if path.is_symlink() or not path.is_file() or path.resolve().parent != folder:
            continue
        with path.open("rb") as stream:
            if stream.read(5) != b"PGDMP":
                continue
        candidates.append(path)
    removed = 0
    for path in sorted(candidates, key=lambda p: p.name, reverse=True)[keep:]:
        if path.resolve() in protected:
            continue
        path.unlink()
        removed += 1
    return removed


def create_database_backup(folder: Path, *, container: str | None = None) -> Path:
    """Публикует .dump только после успешного pg_dump и проверки оглавления."""
    folder = folder.resolve()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
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
        descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=root / "backups")
    parser.add_argument("--container", help="Имя установленного контейнера PostgreSQL")
    args = parser.parse_args()
    path = create_database_backup(args.directory, container=args.container)
    print(f"Архив создан, оглавление проверено: {path}")
    print("Это проверка формата архива; полное восстановление проверяется отдельно.")


if __name__ == "__main__":
    main()
