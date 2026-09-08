"""Запуск Compose без файлов с секретами; Vault сохраняется между запусками."""

import argparse
import json
import os
import secrets
import shutil
import subprocess
import time
from getpass import getpass
from pathlib import Path

import hvac
import requests

ROOT = Path(__file__).resolve().parents[1]
DATABASE_KEYS = ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")


class KeePassCredentials:
    """Ключи Vault зашифрованы в KDBX; мастер-пароль передаётся CLI только через stdin."""

    def __init__(self, database, project, key_file=None):
        self.cli = shutil.which("keepassxc-cli")
        if not self.cli:
            raise RuntimeError("Добавьте keepassxc-cli в PATH.")
        self.database = str(Path(database).resolve(strict=True))
        self.entry = f"DevOps_Project-Vault-{project}"
        self.key_file = str(Path(key_file).resolve(strict=True)) if key_file else None
        self.password = getpass("Мастер-пароль базы KeePassXC (скрытый ввод): ")
        self._run("db-info")

    def _run(self, command, *arguments, value=None):
        args = [self.cli, command, "-q"]
        if self.key_file:
            args += ["-k", self.key_file]
        args += list(arguments) + [self.database]
        if command != "db-info":
            args.append(self.entry)
        content = self.password + "\n"
        if value is not None:
            content += json.dumps(value) + "\n"
        result = subprocess.run(
            args, input=content, text=True, encoding="utf-8", capture_output=True
        )
        if result.returncode:
            raise RuntimeError(
                "KeePassXC не выполнил операцию. Проверьте пароль, файл базы и запись ключей Vault."
            )
        return result.stdout

    def read(self):
        return json.loads(self._run("show", "-s", "-a", "Password").strip())

    def write(self, value):
        # add не перезаписывает существующую запись с ключами другого Vault.
        self._run("add", "-p", value=value)
        if self.read() != value:
            raise RuntimeError(
                "Не удалось проверить сохранённые ключи Vault в KeePassXC."
            )


def legacy_settings(path):
    """Однократный импорт; значения никогда не выводятся и не копируются в файлы."""
    result = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() in DATABASE_KEYS:
            result[key.strip()] = value.strip().strip("\"'")
    if not all(result.get(key) for key in DATABASE_KEYS):
        raise ValueError("В старом .env отсутствуют реквизиты PostgreSQL.")
    return result


def wait_until(check, message, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except (requests.RequestException, hvac.exceptions.VaultError):
            pass
        time.sleep(2)
    raise RuntimeError(message)


def configure_vault(client, initial):
    if "secret/" not in client.sys.list_mounted_secrets_engines()["data"]:
        client.sys.enable_secrets_engine("kv", path="secret", options={"version": "2"})
    try:
        database = client.secrets.kv.v2.read_secret_version(
            path="database/postgres",
            raise_on_deleted_version=True,
        )["data"]["data"]
    except hvac.exceptions.InvalidPath:
        database = {
            "POSTGRES_HOST": "db",
            "POSTGRES_PORT": "5432",
            "POSTGRES_DB": initial.get("POSTGRES_DB", "diabetes"),
            "POSTGRES_USER": initial.get("POSTGRES_USER", "diabetes"),
            "POSTGRES_PASSWORD": initial.get("POSTGRES_PASSWORD")
            or secrets.token_hex(32),
        }
        client.secrets.kv.v2.create_or_update_secret(
            path="database/postgres", secret=database
        )
    if initial and any(initial[key] != database[key] for key in DATABASE_KEYS):
        raise RuntimeError(
            "Реквизиты старой БД отличаются от Vault; автоматическая замена запрещена."
        )
    client.secrets.kv.v2.create_or_update_secret(
        path="kafka/config",
        secret={
            "KAFKA_BOOTSTRAP_SERVERS": "kafka:9092",
            "KAFKA_PREDICTION_TOPIC": "prediction-results",
            "KAFKA_CONSUMER_GROUP": "prediction-results-consumer",
        },
    )
    if "approle/" not in client.sys.list_auth_methods()["data"]:
        client.sys.enable_auth_method("approle")
    identities = {}
    policy = "\n".join(
        f'path "secret/data/{path}" {{ capabilities = ["read"] }}'
        for path in ("database/postgres", "kafka/config")
    )
    for role, prefix in (("diabetes-api", "API"), ("kafka-consumer", "CONSUMER")):
        client.sys.create_or_update_policy(role, policy)
        client.auth.approle.create_or_update_approle(
            role_name=role,
            token_policies=[role],
            token_ttl="1h",
            token_max_ttl="4h",
            secret_id_ttl="0",
            secret_id_num_uses=0,
        )
        # Ротация сервисных удостоверений при управляемом запуске.
        try:
            accessors = client.auth.approle.list_secret_id_accessors(role)["data"][
                "keys"
            ]
        except hvac.exceptions.InvalidPath:
            accessors = []
        for accessor in accessors:
            client.auth.approle.destroy_secret_id_accessor(role, accessor)
        identities[f"{prefix}_VAULT_ROLE_ID"] = client.auth.approle.read_role_id(role)[
            "data"
        ]["role_id"]
        identities[f"{prefix}_VAULT_SECRET_ID"] = (
            client.auth.approle.generate_secret_id(role)["data"]["secret_id"]
        )
    return database, identities


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ci",
        action="store_true",
        help="Одноразовый стенд, bootstrap-ключи только в памяти",
    )
    parser.add_argument(
        "--keepass-db",
        help="Файл KeePassXC .kdbx; мастер-пароль запрашивается скрытым вводом",
    )
    parser.add_argument(
        "--keepass-key-file",
        help="Дополнительный ключевой файл KeePassXC, если используется",
    )
    parser.add_argument(
        "--migrate-env",
        action="store_true",
        help="Перенести существующие реквизиты, после проверки удалить .env",
    )
    parser.add_argument("--project", default="devops_project")
    parser.add_argument("--compose-file", action="append", default=[])
    parser.add_argument("--vault-url", default="http://127.0.0.1:8201")
    parser.add_argument("--api-url", default="http://127.0.0.1:8001")
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument(
        "--check", action="store_true", help="Интеграционные проверки на чистом стенде"
    )
    args = parser.parse_args()
    if args.ci and args.migrate_env:
        parser.error("--ci и --migrate-env несовместимы")
    if not args.ci and not args.keepass_db:
        parser.error("Укажите --keepass-db с путём к базе KeePassXC")
    initial = legacy_settings(ROOT / ".env") if args.migrate_env else {}
    env = dict(os.environ, COMPOSE_DISABLE_ENV_FILE="1")
    for key in ("VAULT_TOKEN", "VAULT_UNSEAL_KEY"):
        env.pop(key, None)
    compose = ["docker", "compose", "-p", args.project, "-f", "docker-compose.yml"]
    for path in args.compose_file:
        compose += ["-f", path]

    def run(*command):
        subprocess.run(compose + list(command), cwd=ROOT, env=env, check=True)

    store = (
        None
        if args.ci
        else KeePassCredentials(args.keepass_db, args.project, args.keepass_key_file)
    )
    if not args.no_build:
        run("build", "diabetes-api", "kafka-consumer")
    run("up", "-d", "vault")
    client = hvac.Client(url=args.vault_url, timeout=10)
    wait_until(lambda: client.sys.read_seal_status() is not None, "Vault не отвечает")
    if not client.sys.is_initialized():
        if store:
            try:
                store.read()
            except RuntimeError:
                pass  # Записи для первого запуска ещё нет.
            else:
                raise RuntimeError(
                    "В KeePassXC уже есть ключи этого проекта, но том Vault пуст. "
                    "Восстановите прежний том или используйте другое имя --project."
                )
        keys = client.sys.initialize(secret_shares=1, secret_threshold=1)
        bootstrap = {
            "root_token": keys["root_token"],
            "unseal_key": keys["keys_base64"][0],
        }
        if store:
            store.write(bootstrap)
    else:
        if args.ci:
            raise RuntimeError(
                "--ci требует нового тома Vault; существующее хранилище не изменено."
            )
        bootstrap = store.read()
    if client.sys.is_sealed():
        client.sys.submit_unseal_key(bootstrap["unseal_key"])
    client.token = bootstrap["root_token"]
    if not client.is_authenticated():
        raise RuntimeError("Не удалось авторизовать настройку Vault.")
    database, identities = configure_vault(client, initial)
    env.update({key: database[key] for key in DATABASE_KEYS})
    env.update(identities)
    if os.getenv("GITHUB_ACTIONS") == "true":
        for value in [
            *bootstrap.values(),
            database["POSTGRES_PASSWORD"],
            *identities.values(),
        ]:
            print(f"::add-mask::{value}", flush=True)
    run("up", "-d", "db", "kafka")
    run(
        "run",
        "--rm",
        "diabetes-api",
        "python",
        "-m",
        "scripts.update_database",
    )
    run(
        "run",
        "--rm",
        "diabetes-api",
        "python",
        "-m",
        "src.register_release",
        "models/current.json",
        "--apply",
    )
    run(
        "run",
        "--rm",
        "diabetes-api",
        "python",
        "-m",
        "scripts.activate_model_release",
        "models/current.json",
        "--if-no-champion",
    )
    run("up", "-d", "--no-build", "diabetes-api", "kafka-consumer")
    wait_until(
        lambda: requests.get(args.api_url + "/db/health", timeout=5).ok, "API не готов"
    )
    if args.check:
        run(
            "exec",
            "-T",
            "diabetes-api",
            "python",
            "-m",
            "scripts.check_database_contract",
        )
        run(
            "exec",
            "-T",
            "diabetes-api",
            "python",
            "-m",
            "scripts.check_prediction_flow",
        )
        service = hvac.Client(url=args.vault_url)
        service.auth.approle.login(
            role_id=identities["API_VAULT_ROLE_ID"],
            secret_id=identities["API_VAULT_SECRET_ID"],
        )
        for path in ("secret/data/database/postgres", "secret/data/kafka/config"):
            assert service.sys.get_capabilities(paths=[path])["data"][
                "capabilities"
            ] == ["read"]
        assert service.sys.get_capabilities(paths=["sys/policies/acl"])["data"][
            "capabilities"
        ] == ["deny"]
        run("restart", "vault")
        wait_until(client.sys.is_sealed, "Vault не перезапустился")
        client.sys.submit_unseal_key(bootstrap["unseal_key"])
        assert (
            client.secrets.kv.v2.read_secret_version(
                path="database/postgres",
                raise_on_deleted_version=True,
            )["data"]["data"]
            == database
        )
        run("restart", "diabetes-api", "kafka-consumer")
        wait_until(
            lambda: requests.get(args.api_url + "/db/health", timeout=5).ok,
            "API не восстановился",
        )
        print("Права AppRole и сохранность Vault после перезапуска проверены.")
    if args.migrate_env:
        (ROOT / ".env").unlink()
        print("Реквизиты перенесены в Vault, проверка БД пройдена, .env удалён.")
    print("Сервисы запущены. Секреты приложения хранятся в Vault.")


if __name__ == "__main__":
    main()
