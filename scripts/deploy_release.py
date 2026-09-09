"""Выкладка проверенного пакета без сборки исходников и файлов с паролями."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from scripts.backup_database import create_database_backup

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "devops_project"
APPLICATION = ["diabetes-api", "kafka-consumer", "metrics-exporter", "frontend"]
MONITORING = ["loki", "prometheus", "grafana", "alloy"]
INFRASTRUCTURE = ["vault", "db", "kafka"]


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
    for service in INFRASTRUCTURE + APPLICATION + MONITORING:
        container = docker_json("inspect", f"{PROJECT}-{service}-1")[0]
        if container["Config"]["Labels"].get("com.docker.compose.project") != PROJECT:
            raise RuntimeError("Контейнер принадлежит другому проекту")
        result[service] = container
    return result


def service_environment(services):
    env = dict(os.environ, COMPOSE_DISABLE_ENV_FILE="1")
    for service, prefix in (("diabetes-api", "API"), ("kafka-consumer", "CONSUMER")):
        values = dict(
            item.split("=", 1)
            for item in services[service]["Config"]["Env"]
            if "=" in item
        )
        for suffix in ("ROLE_ID", "SECRET_ID"):
            value = values.get("VAULT_" + suffix)
            if not value:
                raise RuntimeError("Сервис ещё не настроен для входа в Vault")
            env[f"{prefix}_VAULT_{suffix}"] = value
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
            mount["source"] = feedback["Source"]
    return config


def compose(path, env, *arguments):
    subprocess.run(
        ["docker", "compose", "-p", PROJECT, "-f", str(path), *arguments],
        env=env,
        check=True,
    )


def vault_ready():
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:8201/v1/sys/health", timeout=5
        ) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError):
        return False


def check_application():
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            for address in (
                "http://127.0.0.1:8001/db/health",
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
                    "('http://metrics-exporter:9100/metrics','diabetes_quality_collection_success 1.0')]; "
                    "assert all(marker in urllib.request.urlopen(url,timeout=5).read().decode() for url,marker in checks)",
                ],
                capture_output=True,
                timeout=20,
            )
            if result.returncode:
                raise RuntimeError("Обработчик Kafka или экспортёр метрик не готов")
            return
        except (
            urllib.error.URLError,
            TimeoutError,
            RuntimeError,
            subprocess.TimeoutExpired,
        ):
            time.sleep(2)
    raise RuntimeError("Приложение не подтвердило готовность после обновления")


@contextmanager
def deployment_lock(home):
    home.mkdir(parents=True, exist_ok=True)
    path = home / "deployment.lock"
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError("Другая выкладка уже выполняется") from None
    try:
        os.close(descriptor)
        yield
    finally:
        path.unlink()


def deploy(folder, expected_commit, expected_run, *, preflight=False):
    manifest, config = read_release(folder, expected_commit, expected_run)
    services = existing_services()
    if not vault_ready():
        raise RuntimeError(
            "Vault заблокирован. Выполните .\\start для разблокировки, затем повторите CD"
        )
    env = service_environment(services)
    home = deployment_home()
    with deployment_lock(home):
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
        runtime = configure_runtime(config, destination, services)
        runtime_path = destination / "runtime.json"
        runtime_path.write_text(json.dumps(runtime, indent=2), encoding="utf-8")
        compose(runtime_path, env, "config", "--quiet")
        if preflight:
            print(
                "Пакет, Vault, сервисные реквизиты и конфигурация проверены. Контейнеры не изменены."
            )
            return
        compose(runtime_path, env, "pull", *APPLICATION, *MONITORING, *INFRASTRUCTURE)
        for name in INFRASTRUCTURE:
            image = docker_json("image", "inspect", config["services"][name]["image"])[
                0
            ]
            if image["Id"] != services[name]["Image"]:
                raise RuntimeError(
                    f"Для {name} требуется отдельное обновление инфраструктуры; приложение не изменено"
                )
            if not services[name]["State"]["Running"]:
                raise RuntimeError(
                    f"Сервис {name} остановлен. Сначала выполните .\\start"
                )
        # Обязательно до миграций, регистрации моделей и замены контейнеров.
        # Ошибка копирования прерывает выкладку; восстановления здесь нет.
        backup = create_database_backup(
            home / "backups", container=services["db"]["Id"]
        )
        print(f"Резервная копия БД перед выпуском {manifest['commit']}: {backup}")
        # Долгоживущие Vault/БД/Kafka не пересоздаются при обновлении приложения.
        # Без unseal-ключа нельзя автоматически пересоздать Vault.
        for command in (
            ["scripts.update_database"],
            ["src.register_release", "models/current.json", "--apply"],
            [
                "scripts.activate_model_release",
                "models/current.json",
                "--if-no-champion",
            ],
        ):
            compose(
                runtime_path,
                env,
                "run",
                "--rm",
                "--no-deps",
                "--no-build",
                "diabetes-api",
                "python",
                "-m",
                *command,
            )
        compose(
            runtime_path,
            env,
            "up",
            "-d",
            "--no-deps",
            "--no-build",
            "--wait",
            "--wait-timeout",
            "180",
            *APPLICATION,
            *MONITORING,
        )
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
    subprocess.run(["docker", "start", services["vault"]["Id"]], check=True)
    if not vault_ready():
        database = database or os.getenv("KEEPASS_DB")
        if not database and KEEPASS_PATH_FILE.is_file():
            database = KEEPASS_PATH_FILE.read_text(encoding="utf-8").strip()
        if not database or not Path(database).is_file():
            database = input("Путь к файлу KeePassXC .kdbx: ").strip().strip('"')
        store = KeePassCredentials(database, PROJECT, os.getenv("KEEPASS_KEY_FILE"))
        bootstrap = store.read()
        payload = json.dumps({"key": bootstrap["unseal_key"]}).encode()
        deadline = time.monotonic() + 60
        while True:
            try:
                request = urllib.request.Request(
                    "http://127.0.0.1:8201/v1/sys/unseal",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="PUT",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    sealed = json.load(response)["sealed"]
                if sealed:
                    raise RuntimeError("Vault не разблокирован")
                break
            except urllib.error.URLError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Vault недоступен") from None
                time.sleep(2)
        KEEPASS_PATH_FILE.parent.mkdir(parents=True, exist_ok=True)
        KEEPASS_PATH_FILE.write_text(store.database, encoding="utf-8")
    for name in ["db", "kafka", *APPLICATION, *MONITORING]:
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
