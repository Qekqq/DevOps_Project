"""Read-only inventory and prerequisites for migrating the installed application."""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from scripts import deploy_release as deploy
from scripts.release_provenance import verify_source
from scripts.vault_connection import environment
from scripts.volume_snapshot import SERVICE_VOLUMES

DATA_PATHS = {
    "db": "/var/lib/postgresql/data",
    "vault": "/vault/file",
    "kafka": "/bitnami/kafka",
}


def build_plan(services, current, candidate=None):
    infrastructure = []
    for name, destination in DATA_PATHS.items():
        container = services[name]
        labels = container["Config"].get("Labels") or {}
        if (
            labels.get("com.docker.compose.project") != deploy.PROJECT
            or labels.get("com.docker.compose.service") != name
        ):
            raise RuntimeError(
                "Installed container ownership does not match the deployment"
            )
        mounts = [m for m in container["Mounts"] if m["Destination"] == destination]
        if len(mounts) != 1 or mounts[0]["Type"] != "volume" or not mounts[0]["RW"]:
            raise RuntimeError(
                f"Unexpected data mount for {name}; automatic migration is not supported"
            )
        source = mounts[0]["Name"]
        info = deploy.docker_json("volume", "inspect", source)[0]
        volume_labels = info.get("Labels") or {}
        if (
            volume_labels.get("com.docker.compose.project") != deploy.PROJECT
            or volume_labels.get("com.docker.compose.volume") != SERVICE_VOLUMES[name]
        ):
            raise RuntimeError(f"Unexpected data volume ownership for {name}")
        if info.get("Driver") != "local" or info.get("Options"):
            raise RuntimeError("Migration requires ordinary local volumes")
        record = {
            "service": name,
            "container_id": container["Id"],
            "image_id": container["Image"],
            "data_volume": source,
            "data_path": destination,
            "running": container["State"]["Running"],
            "health": container["State"]
            .get("Health", {})
            .get("Status", "not-configured"),
        }
        if candidate:
            record["target_image"] = candidate["services"][name]["image"]
        infrastructure.append(record)
    clients = []
    for name in ("diabetes-api", "kafka-consumer", "metrics-exporter"):
        env = environment(services[name])
        clients.append(
            {
                "service": name,
                "container_id": services[name]["Id"],
                "image_id": services[name]["Image"],
                "vault_tls": env.get("VAULT_ADDR", "").startswith("https://"),
                "file_identity": all(
                    env.get("VAULT_" + suffix + "_FILE")
                    for suffix in ("ROLE_ID", "SECRET_ID")
                ),
                "database_secret_path": env.get(
                    "VAULT_DB_SECRET_PATH", "database/postgres"
                ),
            }
        )
    identity = {
        "installed_commit": current["commit"],
        "infrastructure": infrastructure,
        "clients": clients,
    }
    fingerprint = hashlib.sha256(
        json.dumps(identity, sort_keys=True).encode()
    ).hexdigest()
    return {
        "format": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "project": deploy.PROJECT,
        "kind": "read-only-plan",
        "fingerprint": fingerprint,
        **identity,
        "ready_to_apply": False,
        "prerequisites": [
            "Получить неизменяемый пакет из успешного CI GitHub и проверить его происхождение перед применением.",
            "Просканировать все целевые образы и устранить уязвимости, блокирующие выпуск.",
            "Использовать make security-migrate RELEASE=путь: read-only план сам по себе ничего не применяет.",
            "До остановки проверить локальный доступ к KeePass и ключу разблокировки установленного Vault.",
            "Получить блокировку выкладки и повторно проверить состав контейнеров и томов.",
            "Проверить свободное место в файловой системе Docker и на физическом диске хоста для снимков, новых томов и pg_dump; для публичного сервера дополнительно настроить внешнюю копию.",
            "Остановить запись приложения, создать и проверить pg_dump, затем остановить инфраструктуру.",
            "Создать и проверить снимки всех трёх остановленных томов; сохранить оригиналы для ручного восстановления.",
            "Восстановить данные в новые тома с прежними cluster ID и основной версией PostgreSQL; подготовить TLS и сервисные реквизиты.",
            "Выполнить миграции административной ролью, затем проверить ограниченные роли приложения и готовность сервисов.",
            "Записать новый установленный выпуск только после проверок; сохранить резервные копии и порядок ручного восстановления.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--release",
        type=Path,
        help="Optional downloaded candidate release; never built or deployed here",
    )
    args = parser.parse_args()
    home = deploy.deployment_home()
    with deploy.deployment_lock(home):
        current = json.loads((home / "current.json").read_text(encoding="utf-8"))
        folder = Path(current["release"]).resolve(strict=True)
        if not folder.is_relative_to((home / "releases").resolve()):
            raise RuntimeError("Installed release is outside the deployment directory")
        deploy.read_release(folder, current["commit"], current["ci_run_id"])
        provenance = None
        candidate = None
        if args.release:
            manifest, candidate = deploy.read_release(args.release)
            provenance = verify_source(args.release, manifest)
        plan = build_plan(deploy.existing_services(), current, candidate)
        if provenance:
            plan["candidate_source"] = provenance
        output = home / "security-migration-plan.json"
        temporary = output.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(output)
    print(f"План сохранён: {output}")
    print(f"Установленный выпуск: {plan['installed_commit'][:7]}")
    for service in plan["infrastructure"]:
        print(
            f"{service['service']}: {service['data_volume']}, running={service['running']}, health={service['health']}"
        )
    print(
        "Контейнеры и данные не изменены. План не заменяет проверки перед применением; обязательные условия перечислены в нём."
    )


if __name__ == "__main__":
    main()
