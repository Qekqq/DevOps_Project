"""Создание пользователя в работающем локальном Docker-стенде."""

import argparse
import getpass
import json
import subprocess
import sys

from src.passwords import hash_password

# Код выполняется внутри API-контейнера и получает доступ к БД через Vault.
# Через stdin передаётся только хеш; секреты не передаются в аргументах команды.
CREATE_USER = """
import json
import sys
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from src.db.database import get_session_factory
from src.db.models import User

data = json.load(sys.stdin)
with get_session_factory()() as db:
    if db.scalar(select(User.id).where(User.username == data['username'])) is not None:
        raise SystemExit('Пользователь уже существует; изменения не внесены.')
    db.add(User(**data, is_active=True))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise SystemExit('Пользователь не создан: нарушено ограничение базы данных.')
    print('Пользователь создан: ' + data['username'] + ', роль: ' + data['role'])
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username")
    parser.add_argument("--role", choices=("user", "admin"), default="user")
    parser.add_argument("--container", default="devops_project-diabetes-api-1")
    args = parser.parse_args()
    if not args.username.strip() or len(args.username) > 100:
        parser.error("Имя пользователя должно содержать от 1 до 100 символов")
    if not sys.stdin.isatty():
        parser.error("Запустите команду в терминале для скрытого ввода пароля")
    password = getpass.getpass("Пароль (15–128 символов, скрытый ввод): ")
    if password != getpass.getpass("Повторите пароль: "):
        parser.error("Пароли не совпадают")
    try:
        encoded = hash_password(password)
    except ValueError as error:
        parser.error(str(error))
    del password
    payload = json.dumps(
        {"username": args.username, "password_hash": encoded, "role": args.role}
    )
    result = subprocess.run(
        ["docker", "exec", "-i", args.container, "python", "-c", CREATE_USER],
        input=payload,
        text=True,
        check=False,
    )
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
