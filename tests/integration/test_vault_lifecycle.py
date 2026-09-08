"""Права сервисов и сохранность секретов после перезапуска Vault в CD."""

import json
import os
import subprocess

import hvac
import requests

from scripts.start_stack import wait_until


def test_vault_lifecycle():
    url = os.environ["TEST_VAULT_URL"]
    client = hvac.Client(url=url, token=os.environ["TEST_VAULT_TOKEN"])
    database = client.secrets.kv.v2.read_secret_version(
        path="database/postgres",
        raise_on_deleted_version=True,
    )["data"]["data"]
    service = hvac.Client(url=url)
    service.auth.approle.login(
        role_id=os.environ["API_VAULT_ROLE_ID"],
        secret_id=os.environ["API_VAULT_SECRET_ID"],
    )
    for path in ("secret/data/database/postgres", "secret/data/kafka/config"):
        assert service.sys.get_capabilities(paths=[path])["data"]["capabilities"] == [
            "read"
        ]
    assert service.sys.get_capabilities(paths=["sys/policies/acl"])["data"][
        "capabilities"
    ] == ["deny"]
    compose = json.loads(os.environ["TEST_COMPOSE_COMMAND"])
    subprocess.run(compose + ["restart", "vault"], check=True)
    wait_until(client.sys.is_sealed, "Vault не перезапустился")
    client.sys.submit_unseal_key(os.environ["TEST_VAULT_UNSEAL_KEY"])
    assert (
        client.secrets.kv.v2.read_secret_version(
            path="database/postgres",
            raise_on_deleted_version=True,
        )["data"]["data"]
        == database
    )
    subprocess.run(compose + ["restart", "diabetes-api", "kafka-consumer"], check=True)
    wait_until(
        lambda: requests.get(os.environ["TEST_API_URL"] + "/db/health", timeout=5).ok,
        "API не восстановился",
    )
