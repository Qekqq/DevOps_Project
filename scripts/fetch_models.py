"""Authenticated DVC download on CI/runners; credentials are never printed."""

import configparser
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    username = os.environ.get("DVC_YANDEX_USER")
    password = os.environ.get("DVC_YANDEX_PASSWORD")
    if not username or not password:
        raise SystemExit("DVC credentials are missing")
    path = ROOT / ".dvc/config.local"
    if path.is_symlink():
        raise SystemExit("DVC config must not be a symlink")
    previous = path.read_bytes() if path.exists() else None
    config = configparser.ConfigParser(interpolation=None)
    section = 'remote "yandex"'
    config[section] = {"user": username, "password": password}
    try:
        with path.open("w", encoding="utf-8") as stream:
            config.write(stream)
        path.chmod(0o600)
        result = subprocess.run(
            [sys.executable, "-m", "dvc", "pull", "models"],
            cwd=ROOT,
            capture_output=True,
        )
        if result.returncode:
            raise SystemExit(
                "Authenticated model download failed; credentials were not logged"
            )
    finally:
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(previous)
    print("Model artifacts downloaded; release hashes must be verified before loading")


if __name__ == "__main__":
    main()
