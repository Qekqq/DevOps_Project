import os

import hvac
from requests import RequestException

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
        raise VaultSecretError(f"Не задана переменная окружения {name}")

    return value


def get_vault_client() -> hvac.Client:
    """
    Создаёт клиент для подключения к Hashicorp Vault.
    """
    vault_addr = get_required_env("VAULT_ADDR")
    client = hvac.Client(url=vault_addr)
    try:
        client.auth.approle.login(
            role_id=get_required_env("VAULT_ROLE_ID"),
            secret_id=get_required_env("VAULT_SECRET_ID"),
        )
        if not client.is_authenticated():
            raise VaultSecretError("Не удалось авторизоваться в Vault")
    except (hvac.exceptions.VaultError, RequestException) as error:
        raise VaultSecretError(
            "Vault недоступен или отклонил авторизацию сервиса"
        ) from error

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
            f"Не удалось прочитать секрет Vault: {secret_path}"
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
        key for key in required_keys if key not in secrets or secrets[key] in (None, "")
    ]

    if missing_keys:
        raise VaultSecretError(
            f"В Vault отсутствуют параметры {secret_name}: {missing_keys}"
        )

    return {key: str(secrets[key]) for key in required_keys}


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
