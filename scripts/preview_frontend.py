"""Копирование локального интерфейса для предпросмотра без сборки образов."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTAINER = "devops_project-frontend-1"
WEB_ROOT = "/usr/share/nginx/html"


def main():
    try:
        status = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", CONTAINER],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if status.returncode or status.stdout.strip() != "true":
            raise RuntimeError(
                "Frontend не запущен или Docker недоступен. Откройте Docker Desktop и выполните make start."
            )
        for name in ("index.html", "styles.css", "app.js", "js", "assets"):
            source = str(ROOT / "frontend" / name)
            if name in ("js", "assets"):
                source += "/."
            subprocess.run(
                ["docker", "cp", source, f"{CONTAINER}:{WEB_ROOT}/{name}"], check=True
            )
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Предпросмотр не обновлён: {error}") from None
    print("Интерфейс обновлён. Нажмите Ctrl+F5 на http://localhost:8080.")
    print("При пересоздании frontend-контейнера вернётся версия из образа.")


if __name__ == "__main__":
    main()
