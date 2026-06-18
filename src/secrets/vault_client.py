import os
from functools import lru_cache

import hvac


REQUIRED_DATABASE_SECRET_KEYS = [
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
]

REQUIRED_KAFKA_SECRET_KEYS = [
    "KAFKA_BOOTSTRAP_SERVERS",
    "KAFKA_PREDICTION_TOPIC",
    "KAFKA_CONSUMER_GROUP",
]


class VaultSecretError(RuntimeError):
    """
    Ошибка получения секретов из Hashicorp Vault.
    """


def get_required_env(name: str) -> str:
    """
    Получает обязательную переменную окружения.
    """
    value = os.getenv(name)

    if not value:
        raise VaultSecretError(f"Environment variable {name} is not set")

    return value


def get_vault_client() -> hvac.Client:
    """
    Создаёт клиент для подключения к Hashicorp Vault.
    """
    vault_addr = get_required_env("VAULT_ADDR")
    vault_token = get_required_env("VAULT_TOKEN")

    client = hvac.Client(
        url=vault_addr,
        token=vault_token,
    )

    if not client.is_authenticated():
        raise VaultSecretError("Vault authentication failed")

    return client


def read_vault_secret(path_env_name: str, default_path: str) -> dict:
    """
    Читает secret из Vault KV v2.
    """
    vault_kv_mount = os.getenv("VAULT_KV_MOUNT", "secret")
    secret_path = os.getenv(path_env_name, default_path)

    client = get_vault_client()

    try:
        response = client.secrets.kv.v2.read_secret_version(
            mount_point=vault_kv_mount,
            path=secret_path,
        )
    except Exception as error:
        raise VaultSecretError(
            f"Failed to read secret from Vault path {secret_path}: {error}"
        ) from error

    return response["data"]["data"]


def validate_secret_keys(
    secrets: dict,
    required_keys: list[str],
    secret_name: str,
) -> dict[str, str]:
    """
    Проверяет, что в секрете есть все обязательные ключи.
    """
    missing_keys = [
        key for key in required_keys
        if key not in secrets or secrets[key] in (None, "")
    ]

    if missing_keys:
        raise VaultSecretError(
            f"Missing {secret_name} secrets in Vault: {missing_keys}"
        )

    return {
        key: str(secrets[key])
        for key in required_keys
    }


@lru_cache
def get_database_secrets() -> dict[str, str]:
    """
    Получает параметры подключения к PostgreSQL из Hashicorp Vault.
    """
    secrets = read_vault_secret(
        path_env_name="VAULT_DB_SECRET_PATH",
        default_path="database/postgres",
    )

    return validate_secret_keys(
        secrets=secrets,
        required_keys=REQUIRED_DATABASE_SECRET_KEYS,
        secret_name="database",
    )


@lru_cache
def get_kafka_secrets() -> dict[str, str]:
    """
    Получает параметры подключения к Kafka из Hashicorp Vault.
    """
    secrets = read_vault_secret(
        path_env_name="VAULT_KAFKA_SECRET_PATH",
        default_path="kafka/config",
    )

    return validate_secret_keys(
        secrets=secrets,
        required_keys=REQUIRED_KAFKA_SECRET_KEYS,
        secret_name="kafka",
    )