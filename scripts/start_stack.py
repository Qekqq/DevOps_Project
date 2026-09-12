"""Запуск Compose без файлов с секретами; Vault сохраняется между запусками."""

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from getpass import getpass
from pathlib import Path

import hvac
import requests

ROOT = Path(__file__).resolve().parents[1]
DATABASE_KEYS = ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")
KEEPASS_PATH_FILE = ROOT / ".local-history" / "keepass-path.txt"


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
            if command == "db-info":
                raise RuntimeError(
                    f"Не удалось открыть базу KeePassXC: {self.database}. "
                    "Проверьте мастер-пароль в KeePassXC и необходимость ключевого файла."
                )
            raise RuntimeError(
                f"База KeePassXC открылась, но операция {command} с записью "
                f"{self.entry} не выполнена. Проверьте наличие и расположение записи."
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


def wait_for_vault_health(compose, env):
    """Ждёт обновления статуса Docker после разблокировки Vault."""
    container_id = subprocess.check_output(
        compose + ["ps", "-q", "vault"], cwd=ROOT, env=env, text=True
    ).strip()
    if not container_id:
        raise RuntimeError("Контейнер Vault не запущен.")
    wait_until(
        lambda: (
            subprocess.check_output(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.Health.Status}}",
                    container_id,
                ],
                env=env,
                text=True,
            ).strip()
            == "healthy"
        ),
        "Docker не подтвердил готовность Vault после разблокировки.",
    )


def validate_service_database(secret, database, username):
    expected = dict(database, POSTGRES_USER=username)
    if (
        any(
            secret.get(key) != expected[key]
            for key in (
                "POSTGRES_HOST",
                "POSTGRES_PORT",
                "POSTGRES_DB",
                "POSTGRES_USER",
            )
        )
        or not isinstance(secret.get("POSTGRES_PASSWORD"), str)
        or len(secret["POSTGRES_PASSWORD"]) < 32
    ):
        raise RuntimeError(
            "Stored service database credentials do not match the restricted role"
        )


def configure_vault(client, initial, *, restricted_database=False):
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
    try:
        client.secrets.kv.v2.read_secret_version(
            path="database/metrics",
            raise_on_deleted_version=True,
        )
    except hvac.exceptions.InvalidPath:
        client.secrets.kv.v2.create_or_update_secret(
            path="database/metrics",
            secret=dict(
                database,
                POSTGRES_USER="diabetes_metrics",
                POSTGRES_PASSWORD=secrets.token_hex(32),
            ),
        )
    if restricted_database:
        for service in ("api", "consumer"):
            path = f"database/{service}"
            try:
                client.secrets.kv.v2.read_secret_version(
                    path=path, raise_on_deleted_version=True
                )
            except hvac.exceptions.InvalidPath:
                client.secrets.kv.v2.create_or_update_secret(
                    path=path,
                    secret=dict(
                        database,
                        POSTGRES_USER=f"diabetes_{service}",
                        POSTGRES_PASSWORD=secrets.token_hex(32),
                    ),
                )
    paths = {"metrics": "diabetes_metrics"}
    if restricted_database:
        paths.update(api="diabetes_api", consumer="diabetes_consumer")
    for name, username in paths.items():
        stored = client.secrets.kv.v2.read_secret_version(
            path=f"database/{name}",
            raise_on_deleted_version=True,
        )["data"]["data"]
        validate_service_database(stored, database, username)
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
    mount_accessor = client.sys.list_auth_methods()["data"]["approle/"]["accessor"]
    identities = {}
    for role, prefix in (
        ("diabetes-api", "API"),
        ("kafka-consumer", "CONSUMER"),
        ("metrics-exporter", "EXPORTER"),
    ):
        paths = ["database/metrics" if prefix == "EXPORTER" else "database/postgres"]
        if restricted_database and prefix in ("API", "CONSUMER"):
            paths = [f"database/{prefix.lower()}"]
        if prefix != "EXPORTER":
            paths.append("kafka/config")
        policy = "\n".join(
            f'path "secret/data/{path}" {{ capabilities = ["read"] }}' for path in paths
        )
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
        # Старые контейнеры могли заблокировать роль повторными отказами входа.
        # Разблокируем только управляемую роль после выдачи нового ключа.
        role_id = identities[f"{prefix}_VAULT_ROLE_ID"]
        client.adapter.post(f"/v1/sys/locked-users/{mount_accessor}/unlock/{role_id}")
    return database, identities


def require_empty_project(project):
    existing = subprocess.check_output(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ],
        text=True,
    ).strip()
    volumes = subprocess.check_output(
        ["docker", "volume", "ls", "-q"],
        text=True,
    ).splitlines()
    if existing or f"{project}_vault_data" in volumes:
        raise RuntimeError(
            "--ci требует пустого отдельного проекта; существующие контейнеры и том Vault не изменены"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ci",
        action="store_true",
        help="Одноразовый стенд, bootstrap-ключи только в памяти",
    )
    parser.add_argument(
        "--keepass-db",
        default=os.getenv("KEEPASS_DB"),
        help="Файл KeePassXC .kdbx; мастер-пароль запрашивается скрытым вводом",
    )
    parser.add_argument(
        "--keepass-key-file",
        help="Дополнительный ключевой файл KeePassXC, если используется",
    )
    parser.add_argument("--project", default="devops_project")
    parser.add_argument("--compose-file", action="append", default=[])
    parser.add_argument(
        "--release-dir", type=Path, help="Проверенный пакет образов для CI"
    )
    parser.add_argument(
        "--local-build",
        action="store_true",
        help="Явно разрешить сборку локального кода вместо выкладки",
    )
    parser.add_argument("--vault-url", default="http://127.0.0.1:8201")
    parser.add_argument(
        "--vault-tls",
        action="store_true",
        help="TLS для нового отдельного стенда Vault",
    )
    parser.add_argument("--api-url", default="http://127.0.0.1:8080/api")
    parser.add_argument(
        "--file-secrets",
        action="store_true",
        help="AppRole в отдельных Docker-томах нового стенда",
    )
    parser.add_argument(
        "--no-build",
        action="store_true",
        help="Использовать готовые Python-образы API, обработчика Kafka и экспортера метрик",
    )
    parser.add_argument(
        "--check", action="store_true", help="Интеграционные проверки на чистом стенде"
    )
    args = parser.parse_args()
    if args.file_secrets and not args.ci:
        parser.error(
            "--file-secrets требует нового стенда --ci; постоянная установка требует отдельной ротации"
        )
    if args.vault_tls and not args.ci:
        parser.error(
            "--vault-tls пока разрешён только с --ci на новом стенде; существующий Vault требует отдельной миграции"
        )
    if not args.ci and not args.local_build:
        parser.error(
            "Для запуска существующей версии используйте make start. Локальная сборка требует --local-build"
        )
    if args.release_dir and not args.ci:
        parser.error(
            "Пакет для постоянного приложения запускается через scripts.deploy_release"
        )
    if args.release_dir:
        from scripts.deploy_release import read_release
        from scripts.model_delivery import stage_models

        manifest, _ = read_release(args.release_dir)
        stage_models(manifest, ROOT / "models", args.release_dir / "models")
        args.no_build = True
    if args.check and not args.ci:
        parser.error("--check разрешён только с --ci на одноразовом стенде")
    if not args.ci and not args.keepass_db:
        if KEEPASS_PATH_FILE.is_file():
            args.keepass_db = KEEPASS_PATH_FILE.read_text(encoding="utf-8").strip()
        if not args.keepass_db or not Path(args.keepass_db).is_file():
            if not sys.stdin.isatty():
                parser.error("Укажите --keepass-db с путём к базе KeePassXC")
            args.keepass_db = input("Путь к файлу KeePassXC .kdbx: ").strip().strip('"')
        if not args.keepass_db or not Path(args.keepass_db).is_file():
            parser.error("Файл KeePassXC не найден. Проверьте путь к .kdbx")
    initial = {}
    env = dict(
        os.environ, COMPOSE_DISABLE_ENV_FILE="1", COMPOSE_PROJECT_NAME=args.project
    )
    if os.name != "nt" and Path("/var/run/docker.sock").exists():
        env["DOCKER_SOCKET_GID"] = str(Path("/var/run/docker.sock").stat().st_gid)
    for key in ("VAULT_TOKEN", "VAULT_UNSEAL_KEY"):
        env.pop(key, None)
    compose_file = (
        str(args.release_dir.resolve() / "docker-compose.json")
        if args.release_dir
        else "docker-compose.yml"
    )
    compose = ["docker", "compose", "-p", args.project, "-f", compose_file]
    for path in args.compose_file:
        compose += ["-f", path]
    if args.ci:
        require_empty_project(args.project)

    def run(*command):
        subprocess.run(compose + list(command), cwd=ROOT, env=env, check=True)

    store = (
        None
        if args.ci
        else KeePassCredentials(args.keepass_db, args.project, args.keepass_key_file)
    )
    if store:
        # Запоминаем только путь после успешного открытия KeePass. Пароль и
        # ключи сюда не записываются; .local-history исключена из Git и образа.
        KEEPASS_PATH_FILE.parent.mkdir(parents=True, exist_ok=True)
        KEEPASS_PATH_FILE.write_text(store.database, encoding="utf-8")
    if not args.no_build:
        run(
            "build",
            "diabetes-api",
            "kafka-consumer",
            "metrics-exporter",
            "docker-stats",
        )
    if not args.no_build:
        run("build", "frontend", "grafana", "prometheus", "loki", "alloy")
    if args.vault_tls:
        from scripts.vault_tls import prepare_tls

        rendered = json.loads(
            subprocess.check_output(
                compose + ["config", "--format", "json"],
                env=env,
                cwd=ROOT,
                text=True,
            )
        )
        ca_file = prepare_tls(
            args.project,
            rendered["services"]["diabetes-api"].get(
                "image", f"{args.project}-diabetes-api"
            ),
            ROOT / ".local-history" / "vault-tls" / args.project,
        )
        env["VAULT_CA_FILE"] = str(ca_file)
        compose += ["-f", str((args.release_dir or ROOT) / "vault/tls.compose.yml")]
        args.vault_url = args.vault_url.replace("http://", "https://", 1)
    run("up", "-d", "vault")
    client = hvac.Client(
        url=args.vault_url, timeout=10, verify=env.get("VAULT_CA_FILE") or True
    )
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
    wait_for_vault_health(compose, env)
    client.token = bootstrap["root_token"]
    if not client.is_authenticated():
        raise RuntimeError("Не удалось авторизовать настройку Vault.")
    # Останавливаем обращения со старыми ключами до их отзыва в Vault.
    run("stop", "diabetes-api", "kafka-consumer", "metrics-exporter")
    database, identities = configure_vault(client, initial, restricted_database=args.ci)
    env.update({key: database[key] for key in DATABASE_KEYS})
    env.update(identities)
    if args.ci:
        env["API_VAULT_DB_SECRET_PATH"] = "database/api"
        env["CONSUMER_VAULT_DB_SECRET_PATH"] = "database/consumer"
    if os.getenv("GITHUB_ACTIONS") == "true":
        for value in [
            *bootstrap.values(),
            database["POSTGRES_PASSWORD"],
            *identities.values(),
        ]:
            print(f"::add-mask::{value}", flush=True)
    if args.file_secrets:
        from scripts.vault_identity import write_identities

        rendered = json.loads(
            subprocess.check_output(
                compose + ["config", "--format", "json"], cwd=ROOT, env=env, text=True
            )
        )
        helper_image = rendered["services"]["diabetes-api"].get(
            "image", f"{args.project}-diabetes-api"
        )
        overlay = write_identities(args.project, helper_image, identities)
        identity_config = (
            ROOT / ".local-history" / "vault-identities" / args.project / "compose.json"
        )
        identity_config.parent.mkdir(parents=True, exist_ok=True)
        identity_config.write_text(json.dumps(overlay, indent=2), encoding="utf-8")
        compose += ["-f", str(identity_config)]
        for key in identities:
            env.pop(key, None)
    run("up", "-d", "--wait", "--wait-timeout", "180", "db", "kafka")

    def maintenance(command, extra=None):
        from scripts.maintenance_client import run_maintenance

        run_maintenance(compose, env, database, command, extra=extra)

    maintenance(["scripts.update_database"])
    metrics_credentials = client.secrets.kv.v2.read_secret_version(
        path="database/metrics",
        raise_on_deleted_version=True,
    )["data"]["data"]
    runtime_credentials = {}
    for service in ("api", "consumer"):
        if args.ci:
            credentials = client.secrets.kv.v2.read_secret_version(
                path=f"database/{service}", raise_on_deleted_version=True
            )["data"]["data"]
            runtime_credentials[f"diabetes_{service}"] = credentials[
                "POSTGRES_PASSWORD"
            ]
        else:
            runtime_credentials[f"diabetes_{service}"] = secrets.token_hex(32)
    maintenance(
        ["provision-runtime"],
        {"identities": runtime_credentials, "metrics": metrics_credentials},
    )
    maintenance(["src.register_release", "models/current.json", "--apply"])
    maintenance(
        ["scripts.activate_model_release", "models/current.json", "--if-no-champion"]
    )
    run(
        "up",
        "-d",
        "--no-build",
        "--wait",
        "--wait-timeout",
        "180",
        "diabetes-api",
        "kafka-consumer",
        "metrics-exporter",
        "prometheus",
        "grafana",
        "alloy",
        "docker-stats",
        "frontend",
    )
    wait_until(
        lambda: requests.get(args.api_url + "/db/health", timeout=5).ok, "API не готов"
    )
    if args.check:
        test_packages = ROOT / ".local-history/integration-packages"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--upgrade",
                "--target",
                str(test_packages),
                "-r",
                str(ROOT / "tests/requirements.txt"),
            ],
            check=True,
        )
        subprocess.run(
            compose
            + [
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "-v",
                f"{ROOT / 'tests'}:/app/tests:ro",
                "-v",
                f"{test_packages}:/test-packages:ro",
                "-e",
                "RUN_INTEGRATION_TESTS=1",
                "diabetes-api",
                "python",
                "-m",
                "scripts.database_maintenance",
                "integration-check",
            ],
            input=json.dumps({"database": database}),
            env=env,
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            check=True,
        )
        test_env = dict(
            env,
            RUN_INTEGRATION_TESTS="1",
            TEST_VAULT_URL=args.vault_url,
            TEST_VAULT_CACERT=env.get("VAULT_CA_FILE", ""),
            TEST_API_URL=args.api_url,
            TEST_VAULT_TOKEN=bootstrap["root_token"],
            TEST_VAULT_UNSEAL_KEY=bootstrap["unseal_key"],
            TEST_VAULT_FILE_SECRETS="1" if args.file_secrets else "0",
            TEST_RESTRICTED_DATABASE="1",
            **(identities if args.file_secrets else {}),
            TEST_COMPOSE_COMMAND=json.dumps(compose),
        )
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/integration/test_database_roles.py",
                "tests/integration/test_vault_lifecycle.py",
                "tests/integration/test_monitoring_boundary.py",
                "-v",
            ],
            cwd=ROOT,
            env=test_env,
            check=True,
        )
    print("Сервисы запущены. Секреты приложения хранятся в Vault.")


if __name__ == "__main__":
    main()
