"""Сохранение доступа к DVC в локальном файле, исключённом из Git."""

import configparser
import argparse
import os
from getpass import getpass
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-env", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    path = root / ".dvc" / "config.local"
    if args.from_env:
        username = os.environ.get("DVC_YANDEX_USER", "").strip()
        password = os.environ.get("DVC_YANDEX_PASSWORD", "")
    else:
        username = input("Логин Яндекса: ").strip()
        password = getpass("Пароль приложения «Файлы WebDAV» (ввод скрыт): ")
    if not username or not password:
        raise SystemExit("Логин и пароль не должны быть пустыми")
    if any(character.isspace() for character in username):
        raise SystemExit("Введите логин Яндекс ID без пробелов, а не отображаемое имя")
    config = configparser.ConfigParser(interpolation=None)
    if path.exists():
        config.read(path, encoding="utf-8")
    section = 'remote "yandex"'
    if not config.has_section(section):
        config.add_section(section)
    config.set(section, "user", username)
    config.set(section, "password", password)
    with path.open("w", encoding="utf-8") as file:
        config.write(file)
    print("Доступ сохранён в .dvc/config.local. Файл исключён из Git.")
    print("Пароль хранится локально открытым текстом; не публикуйте этот файл.")


if __name__ == "__main__":
    main()
