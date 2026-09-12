"""Выкладка проверенного пакета без сборки исходников и файлов с паролями."""

import argparse
import hashlib
import http.client
import json
import os
import re
import shutil
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from scripts.backup_database import create_database_backup
from scripts.maintenance_client import installed_admin_credentials, run_maintenance
from scripts.model_delivery import stage_models
from scripts.vault_connection import (
    host_bind_source,
    installed_connection,
    preserve_transport,
)
from scripts.vault_identity import preserve_identities

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "devops_project"
APPLICATION = ["diabetes-api", "kafka-consumer", "metrics-exporter", "frontend"]
MONITORING = ["loki", "prometheus", "grafana", "alloy"]
OPTIONAL_MONITORING = ["docker-proxy", "docker-stats"]
INFRASTRUCTURE = ["vault", "db", "kafka"]
HTTP_CONNECTION_ERRORS = (
    urllib.error.URLError,
    TimeoutError,
    ConnectionError,
    http.client.HTTPException,
)


def deployment_home():
    return Path(
        os.environ.get("DEVOPS_DEPLOY_HOME", ROOT / ".local-history" / "deployment")
    ).resolve()


def read_release(folder, expected_commit=None, expected_run=None):
    folder = Path(folder).resolve(strict=True)
    manifest = json.loads((folder / "release.json").read_text(encoding="utf-8"))
    if not re.fullmatch(r"[0-9a-f]{40}", manifest["commit"]):
        raise ValueError("Некорректный SHA выпуска")
    if expected_commit and manifest["commit"] != expected_commit:
        raise ValueError("Пакет относится к другому коммиту")
    if expected_run and manifest["ci_run_id"] != str(expected_run):
        raise ValueError("Пакет относится к другому запуску CI")
    for name, expected in manifest["files"].items():
        path = (folder / name).resolve(strict=True)
        if (
            not path.is_relative_to(folder)
            or hashlib.sha256(path.read_bytes()).hexdigest() != expected
        ):
            raise ValueError("Изменён файл пакета выкладки")
    if "docker-compose.json" not in manifest["files"]:
        raise ValueError("В пакете нет проверенной конфигурации Compose")
    config = json.loads((folder / "docker-compose.json").read_text(encoding="utf-8"))
    for service in config["services"].values():
        if "build" in service or not re.fullmatch(
            r"[a-z0-9./:_-]+@sha256:[0-9a-f]{64}", service["image"]
        ):
            raise ValueError("Разрешены только готовые образы по digest")
    return manifest, config


def docker_json(*arguments):
    # Вывод inspect может содержать секреты: только память, не журнал CI.
    result = subprocess.run(
        ["docker", *arguments], capture_output=True, text=True, encoding="utf-8"
    )
    if result.returncode:
        raise RuntimeError("Docker недоступен или отсутствует настроенный контейнер")
    return json.loads(result.stdout)


def existing_services():
    result = {}
    optional = []
    for name in OPTIONAL_MONITORING:
        ids = subprocess.check_output(
            ["docker", "ps", "-aq", "--filter", f"name=^/{PROJECT}-{name}-1$"],
            text=True,
        ).strip()
        if ids:
            optional.append(name)
    for service in INFRASTRUCTURE + APPLICATION + MONITORING + optional:
        container = docker_json("inspect", f"{PROJECT}-{service}-1")[0]
        if container["Config"]["Labels"].get("com.docker.compose.project") != PROJECT:
            raise RuntimeError("Контейнер принадлежит другому проекту")
        result[service] = container
    return result


def service_environment(services):
    env = dict(os.environ, COMPOSE_DISABLE_ENV_FILE="1")
    for prefix in ("API", "CONSUMER", "EXPORTER"):
        for suffix in ("ROLE_ID", "SECRET_ID"):
            env.pop(f"{prefix}_VAULT_{suffix}", None)
    socket_groups = (
        services.get("docker-proxy", {}).get("HostConfig", {}).get("GroupAdd")
    )
    if socket_groups:
        env["DOCKER_SOCKET_GID"] = socket_groups[0]
    for service, prefix in (("diabetes-api", "API"), ("kafka-consumer", "CONSUMER")):
        values = dict(
            item.split("=", 1)
            for item in services[service]["Config"]["Env"]
            if "=" in item
        )
        env[f"{prefix}_VAULT_DB_SECRET_PATH"] = values.get(
            "VAULT_DB_SECRET_PATH", "database/postgres"
        )
        for suffix in ("ROLE_ID", "SECRET_ID"):
            if values.get("VAULT_" + suffix + "_FILE"):
                continue
            value = values.get("VAULT_" + suffix)
            if not value:
                raise RuntimeError("Сервис ещё не настроен для входа в Vault")
            env[f"{prefix}_VAULT_{suffix}"] = value
    # Preserve a dedicated exporter identity once provisioned. Older installations
    # retain their existing identity until the explicit Vault role migration.
    exporter = services.get("metrics-exporter", {}).get("Config", {}).get("Env", [])
    exporter_env = dict(item.split("=", 1) for item in exporter if "=" in item)
    env["EXPORTER_VAULT_DB_SECRET_PATH"] = exporter_env.get(
        "VAULT_DB_SECRET_PATH", "database/postgres"
    )
    for suffix in ("ROLE_ID", "SECRET_ID"):
        if exporter_env.get("VAULT_" + suffix + "_FILE"):
            continue
        env[f"EXPORTER_VAULT_{suffix}"] = exporter_env.get(
            "VAULT_" + suffix
        ) or env.get(f"API_VAULT_{suffix}", "")
        if not env[f"EXPORTER_VAULT_{suffix}"]:
            raise RuntimeError(
                "Exporter AppRole identity is missing; complete its migration"
            )
    values = dict(
        item.split("=", 1) for item in services["db"]["Config"]["Env"] if "=" in item
    )
    for key in ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"):
        env[key] = values[key]
    # Не наследуем случайную локальную настройку адреса приложения.
    for key in (
        "PUBLIC_ORIGIN",
        "SESSION_COOKIE_SECURE",
        "COMPOSE_FILE",
        "COMPOSE_PROJECT_NAME",
    ):
        env.pop(key, None)
    return env


def configure_runtime(config, folder, services):
    config = json.loads(json.dumps(config))
    # A completed infrastructure migration may use new explicitly named volumes.
    # Carry those mounts forward instead of reverting to the old Compose names.
    paths = {
        "db": ("pgdata", "/var/lib/postgresql/data"),
        "vault": ("vault_data", "/vault/file"),
        "kafka": ("kafka_data", "/bitnami/kafka"),
    }
    for name, (logical, target) in paths.items():
        if name not in config["services"] or name not in services:
            continue
        mounts = [
            m for m in services[name].get("Mounts", []) if m["Destination"] == target
        ]
        if not mounts:
            continue
        if len(mounts) != 1 or mounts[0]["Type"] != "volume":
            raise RuntimeError("Unexpected installed infrastructure data mount")
        config.setdefault("volumes", {})[logical] = {
            "name": mounts[0]["Name"],
            "external": True,
        }
        for mount in config["services"][name].get("volumes", []):
            if mount["target"] == target:
                mount.update(type="volume", source=logical)
    kafka = services.get("kafka")
    if kafka and "kafka" in config["services"]:
        settings = dict(
            item.split("=", 1)
            for item in kafka.get("Config", {}).get("Env", [])
            if "=" in item
        )
        if settings.get("CLUSTER_ID"):
            config["services"]["kafka"].setdefault("environment", {})["CLUSTER_ID"] = (
                settings["CLUSTER_ID"]
            )
    feedback = next(
        mount
        for mount in services["diabetes-api"]["Mounts"]
        if mount["Destination"] == "/app/data/feedback"
    )
    for service in config["services"].values():
        for mount in service.get("volumes", []):
            if mount["type"] == "bind" and mount["source"].startswith("."):
                mount["source"] = str((folder / mount["source"]).resolve())
    for mount in config["services"]["diabetes-api"]["volumes"]:
        if mount["target"] == "/app/data/feedback":
            mount["source"] = host_bind_source(feedback["Source"])
    preserve_transport(config, services)
    preserve_identities(config, services, PROJECT)
    return config


def compose(path, env, *arguments):
    subprocess.run(
        ["docker", "compose", "-p", PROJECT, "-f", str(path), *arguments],
        env=env,
        check=True,
    )


def vault_ready(services):
    connection = installed_connection(services)
    try:
        with connection.opener.open(
            connection.url + "sys/health", timeout=5
        ) as response:
            return response.status == 200
    except HTTP_CONNECTION_ERRORS as error:
        if isinstance(error, urllib.error.URLError) and isinstance(
            error.reason, ssl.SSLCertVerificationError
        ):
            raise RuntimeError(
                "Vault TLS certificate verification failed; trust settings were not changed"
            ) from None
        return False


def wait_for_vault_response(request, timeout=60, *, opener=None):
    """Ждёт HTTP API после запуска контейнера, включая ранний обрыв соединения."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            with (opener.open if opener else urllib.request.urlopen)(
                request, timeout=5
            ) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code < 500:
                # Не выводим тело запроса разблокировки или ответ с секретами.
                raise RuntimeError(
                    f"Vault отклонил запрос (HTTP {error.code}); проверьте настройки и ключи"
                ) from None
        except HTTP_CONNECTION_ERRORS as error:
            if isinstance(error, urllib.error.URLError) and isinstance(
                error.reason, ssl.SSLCertVerificationError
            ):
                raise RuntimeError(
                    "Vault TLS certificate verification failed; trust settings were not changed"
                ) from None
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "Vault не ответил вовремя. Проверьте контейнер Vault и повторите make start"
            ) from None
        time.sleep(2)


def check_infrastructure(config, services):
    """Проверяет текущие образы до загрузки выпуска и любых изменений БД."""
    for name in INFRASTRUCTURE:
        container = services[name]
        if not container["State"]["Running"]:
            raise RuntimeError(
                f"Сервис {name} остановлен. Сначала выполните make start"
            )
        if container["State"].get("Health", {}).get("Status") in (
            "starting",
            "unhealthy",
        ):
            raise RuntimeError(f"Сервис {name} ещё не подтвердил готовность")
        image = docker_json("image", "inspect", container["Image"])[0]
        expected = config["services"][name]["image"].split("@", 1)[1]
        # RepoDigests may identify a multi-platform index, while Id identifies
        # the image for this host. Do not compare only these different IDs.
        digests = {value.rsplit("@", 1)[-1] for value in image.get("RepoDigests", [])}
        digests.add(image["Id"])
        if expected not in digests:
            raise RuntimeError(
                f"Образ {name} отличается от проверяемого выпуска: "
                f"текущий {container['Image']}, ожидается {expected}. "
                "Требуется отдельное обновление инфраструктуры; приложение не изменено"
            )


def check_application():
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            for address in (
                "http://127.0.0.1:8080/api/db/health",
                "http://127.0.0.1:8080/healthz",
            ):
                with urllib.request.urlopen(address, timeout=5) as response:
                    if response.status != 200:
                        raise RuntimeError("Сервис не готов")
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    f"{PROJECT}-diabetes-api-1",
                    "python",
                    "-c",
                    "import urllib.request; "
                    "checks=[('http://kafka-consumer:9101/metrics','diabetes_consumer_connected 1.0'),"
                    "('http://metrics-exporter:9100/metrics','diabetes_container_memory_bytes ')]; "
                    "assert all(marker in urllib.request.urlopen(url,timeout=5).read().decode() for url,marker in checks)",
                ],
                capture_output=True,
                timeout=20,
            )
            if result.returncode:
                raise RuntimeError("Обработчик Kafka или экспортёр метрик не готов")
            return
        except (
            *HTTP_CONNECTION_ERRORS,
            RuntimeError,
            subprocess.TimeoutExpired,
        ):
            time.sleep(2)
    raise RuntimeError("Приложение не подтвердило готовность после обновления")


@contextmanager
def deployment_lock(home, *, allow_pending_migration=False):
    home.mkdir(parents=True, exist_ok=True)
    path = home / "deployment.lock"
    # Lock the open file, not its existence. The OS releases this lock even
    # after a killed process; never unlink it (other processes may have it open).
    stream = path.open("a+b")
    try:
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            import errno

            if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
            raise RuntimeError("Другая выкладка уже выполняется") from None
        if (
            not allow_pending_migration
            and (home / "security-migration.pending.json").exists()
        ):
            raise RuntimeError(
                "Не завершён перенос безопасности. Сначала выполните ручное восстановление; make start и CD приостановлены"
            )
        yield
    finally:
        stream.close()


def prepare_application_storage(runtime_path, env, services):
    # Existing volumes may contain root-owned logs from earlier releases.
    # Restrict ownership changes to the two application data mounts.
    compose(
        runtime_path,
        env,
        "run",
        "--rm",
        "--no-deps",
        "--pull",
        "never",
        "--user",
        "0:0",
        "--cap-add",
        "CHOWN",
        "--cap-add",
        "DAC_OVERRIDE",
        "diabetes-api",
        "python",
        "-c",
        "import os; from pathlib import Path; "
        "roots=[Path('/app/logs'),Path('/app/data/feedback')]; "
        "[(os.chown(p,10001,10001,follow_symlinks=False)) for r in roots for p in [r,*r.rglob('*')]]",
    )
    for mount in services.get("alloy", {}).get("Mounts", []):
        if mount["Destination"] == "/var/lib/alloy" and mount["Type"] == "volume":
            compose(
                runtime_path,
                env,
                "run",
                "--rm",
                "--no-deps",
                "--pull",
                "never",
                "--user",
                "0:0",
                "--cap-add",
                "CHOWN",
                "--cap-add",
                "DAC_OVERRIDE",
                "-v",
                mount["Name"] + ":/alloy-state",
                "diabetes-api",
                "python",
                "-c",
                "import os; from pathlib import Path; r=Path('/alloy-state'); "
                "[os.chown(p,473,473,follow_symlinks=False) for p in [r,*r.rglob('*')]]",
            )
    # Долгоживущие Vault/БД/Kafka не пересоздаются при обновлении приложения.
    # Без unseal-ключа нельзя автоматически пересоздать Vault.


def deploy(folder, expected_commit, expected_run, *, preflight=False):
    manifest, config = read_release(folder, expected_commit, expected_run)
    home = deployment_home()
    with deployment_lock(home):
        services = existing_services()
        if not vault_ready(services):
            raise RuntimeError(
                "Vault заблокирован. Выполните make start для разблокировки, затем повторите CD"
            )
        check_infrastructure(config, services)
        env = service_environment(services)
        package_id = hashlib.sha256(
            json.dumps(manifest, sort_keys=True).encode()
        ).hexdigest()[:12]
        destination = home / "releases" / f"{manifest['commit']}-{package_id}"
        if destination.exists():
            old_manifest, _ = read_release(destination, expected_commit)
            if old_manifest != manifest:
                raise ValueError("В локальном каталоге уже другой пакет этого коммита")
        else:
            shutil.copytree(folder, destination)
        stage_models(manifest, ROOT / "models", destination / "models")
        runtime = configure_runtime(config, destination, services)
        runtime_path = destination / "runtime.json"
        runtime_path.write_text(json.dumps(runtime, indent=2), encoding="utf-8")
        compose(runtime_path, env, "config", "--quiet")
        if preflight:
            print(
                "Пакет, образы инфраструктуры, Vault, сервисные реквизиты и конфигурация проверены. Контейнеры не изменены."
            )
            return
        print("Получение проверенных образов приложения и мониторинга...")
        compose(
            runtime_path,
            env,
            "pull",
            *APPLICATION,
            *MONITORING,
            *(name for name in OPTIONAL_MONITORING if name in config["services"]),
        )
        # Обязательно до миграций, регистрации моделей и замены контейнеров.
        # Ошибка копирования прерывает выкладку; восстановления здесь нет.
        print("Создание резервной копии БД перед обновлением...")
        backup = create_database_backup(
            home / "backups", container=services["db"]["Id"]
        )
        print(f"Резервная копия БД перед выпуском {manifest['commit']}: {backup}")
        prepare_application_storage(runtime_path, env, services)
        for command in (
            ["scripts.update_database"],
            ["src.register_release", "models/current.json", "--apply"],
            [
                "scripts.activate_model_release",
                "models/current.json",
                "--if-no-champion",
            ],
        ):
            print(f"Выполнение {command[0]}...")
            run_maintenance(
                ["docker", "compose", "-p", PROJECT, "-f", str(runtime_path)],
                env,
                installed_admin_credentials(services),
                command,
            )
        print("Обновление контейнеров приложения и мониторинга...")
        compose(
            runtime_path,
            env,
            "up",
            "-d",
            "--no-deps",
            "--no-build",
            "--pull",
            "never",
            "--wait",
            "--wait-timeout",
            "180",
            *APPLICATION,
            *MONITORING,
            *(name for name in OPTIONAL_MONITORING if name in config["services"]),
        )
        print("Проверка готовности обновлённого приложения...")
        check_application()
        state = {
            "commit": manifest["commit"],
            "ci_run_id": manifest["ci_run_id"],
            "release": str(destination),
            "database_backup": str(backup),
        }
        temporary = home / "current.tmp"
        temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
        temporary.replace(home / "current.json")
        print(f"Приложение обновлено до {manifest['commit']}. Готовность подтверждена.")


def resume(database=None):
    # Ручное действие после включения ПК. Только существующие контейнеры,
    # без сборки и без использования исходников для их пересоздания.
    from scripts.start_stack import KEEPASS_PATH_FILE, KeePassCredentials

    services = existing_services()
    connection = installed_connection(services)
    subprocess.run(["docker", "start", services["vault"]["Id"]], check=True)
    print("Ожидание готовности Vault...")
    status = wait_for_vault_response(
        connection.url + "sys/seal-status", opener=connection.opener
    )
    if not status["initialized"]:
        raise RuntimeError(
            "Vault не инициализирован; проверьте подключение прежнего тома данных"
        )
    if status["sealed"]:
        database = database or os.getenv("KEEPASS_DB")
        if not database and KEEPASS_PATH_FILE.is_file():
            database = KEEPASS_PATH_FILE.read_text(encoding="utf-8").strip()
        if not database or not Path(database).is_file():
            database = input("Путь к файлу KeePassXC .kdbx: ").strip().strip('"')
        store = KeePassCredentials(database, PROJECT, os.getenv("KEEPASS_KEY_FILE"))
        bootstrap = store.read()
        payload = json.dumps({"key": bootstrap["unseal_key"]}).encode()
        request = urllib.request.Request(
            connection.url + "sys/unseal",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        if wait_for_vault_response(request, opener=connection.opener)["sealed"]:
            raise RuntimeError("Vault не разблокирован")
        KEEPASS_PATH_FILE.parent.mkdir(parents=True, exist_ok=True)
        KEEPASS_PATH_FILE.write_text(store.database, encoding="utf-8")
    for name in [
        "db",
        "kafka",
        *APPLICATION,
        *MONITORING,
        *(name for name in OPTIONAL_MONITORING if name in services),
    ]:
        subprocess.run(["docker", "start", f"{PROJECT}-{name}-1"], check=True)
    check_application()
    print("Существующая версия запущена. Локальный код не собирался.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path)
    parser.add_argument("--commit")
    parser.add_argument("--ci-run")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--keepass-db", help="Путь к KeePass при ручном запуске")
    args = parser.parse_args()
    try:
        if args.resume and not args.release:
            with deployment_lock(deployment_home()):
                resume(args.keepass_db)
        elif args.release and args.commit and args.ci_run and not args.resume:
            deploy(args.release, args.commit, args.ci_run, preflight=args.preflight)
        else:
            parser.error("Укажите --resume либо --release, --commit и --ci-run")
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        # Не выводим traceback с локальными переменными или выводом inspect.
        raise SystemExit(f"Обновление не завершено: {error}") from None


if __name__ == "__main__":
    main()
