"""Создание пользователя в работающем локальном Docker-стенде."""

import argparse
import getpass
import json
import subprocess
import sys

from src.passwords import hash_password


def create_record(data):
    """Runs only in the administrator maintenance process."""
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from src.db.database import get_session_factory
    from src.db.models import User

    if set(data) != {"username", "password_hash", "role"} or data["role"] not in {
        "user",
        "admin",
    }:
        raise ValueError("Invalid user record")
    with get_session_factory()() as db:
        if (
            db.scalar(select(User.id).where(User.username == data["username"]))
            is not None
        ):
            raise ValueError("User already exists")
        db.add(User(**data, is_active=True))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise ValueError("User could not be created") from None


def provision_user(container, user):
    """Docker administrator sends credentials via stdin, never to the API process."""
    from scripts.maintenance_client import installed_admin_credentials

    def inspect(name):
        return json.loads(subprocess.check_output(["docker", "inspect", name]))[0]

    api = inspect(container)
    labels = api["Config"].get("Labels") or {}
    project = labels.get("com.docker.compose.project")
    if not project or labels.get("com.docker.compose.service") != "diabetes-api":
        raise ValueError("Expected the application's API container")
    database = inspect(f"{project}-db-1")
    db_labels = database["Config"].get("Labels") or {}
    if (
        db_labels.get("com.docker.compose.project") != project
        or db_labels.get("com.docker.compose.service") != "db"
    ):
        raise ValueError("Database does not belong to the application")
    payload = {"database": installed_admin_credentials({"db": database}), "user": user}
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "--pull",
            "never",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--network",
            f"container:{api['Id']}",
            "--entrypoint",
            "python",
            api["Image"],
            "-m",
            "scripts.database_maintenance",
            "create-user",
        ],
        input=json.dumps(payload),
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError(
            "Не удалось создать пользователя; проверьте имя и версию установленного приложения"
        )


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
    try:
        provision_user(
            args.container,
            {"username": args.username, "password_hash": encoded, "role": args.role},
        )
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError):
        raise SystemExit(
            "Пользователь не создан. Проверьте имя и готовность установленного выпуска."
        ) from None
    print(f"Пользователь создан: {args.username}, роль: {args.role}")


if __name__ == "__main__":
    main()
