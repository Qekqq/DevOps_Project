"""Права сервисов и сохранность секретов после перезапуска Vault в CD."""

import json
import os
import subprocess
from pathlib import Path

import hvac
import pytest
import requests

from scripts.start_stack import configure_vault, wait_for_vault_health, wait_until


def test_vault_tls_rejects_untrusted_connections():
    url = os.environ["TEST_VAULT_URL"]
    if not url.startswith("https://"):
        pytest.skip("TLS migration is tested on a separate HTTPS stack")
    with pytest.raises(requests.exceptions.SSLError):
        requests.get(url + "/v1/sys/health", timeout=5)
    assert (
        requests.get(
            url + "/v1/sys/health", verify=os.environ["TEST_VAULT_CACERT"], timeout=5
        ).status_code
        == 200
    )


def test_vault_lifecycle():
    url = os.environ["TEST_VAULT_URL"]
    client = hvac.Client(
        url=url,
        token=os.environ["TEST_VAULT_TOKEN"],
        verify=os.getenv("TEST_VAULT_CACERT") or True,
    )
    database = client.secrets.kv.v2.read_secret_version(
        path="database/postgres",
        raise_on_deleted_version=True,
    )["data"]["data"]
    service = hvac.Client(url=url, verify=os.getenv("TEST_VAULT_CACERT") or True)
    service.auth.approle.login(
        role_id=os.environ["API_VAULT_ROLE_ID"],
        secret_id=os.environ["API_VAULT_SECRET_ID"],
    )
    api_path = (
        "database/api"
        if os.getenv("TEST_RESTRICTED_DATABASE") == "1"
        else "database/postgres"
    )
    for path in (f"secret/data/{api_path}", "secret/data/kafka/config"):
        assert service.sys.get_capabilities(paths=[path])["data"]["capabilities"] == [
            "read"
        ]
    assert service.sys.get_capabilities(paths=["sys/policies/acl"])["data"][
        "capabilities"
    ] == ["deny"]
    exporter = hvac.Client(url=url, verify=os.getenv("TEST_VAULT_CACERT") or True)
    exporter.auth.approle.login(
        role_id=os.environ["EXPORTER_VAULT_ROLE_ID"],
        secret_id=os.environ["EXPORTER_VAULT_SECRET_ID"],
    )
    assert exporter.sys.get_capabilities(paths=["secret/data/kafka/config"])["data"][
        "capabilities"
    ] == ["deny"]
    assert exporter.sys.get_capabilities(paths=["secret/data/database/postgres"])[
        "data"
    ]["capabilities"] == ["deny"]
    assert (
        exporter.secrets.kv.v2.read_secret_version(
            path="database/metrics", raise_on_deleted_version=True
        )["data"]["data"]["POSTGRES_USER"]
        == "diabetes_metrics"
    )
    compose = json.loads(os.environ["TEST_COMPOSE_COMMAND"])
    subprocess.run(compose + ["restart", "vault"], check=True)
    wait_until(client.sys.is_sealed, "Vault не перезапустился")
    client.sys.submit_unseal_key(os.environ["TEST_VAULT_UNSEAL_KEY"])
    wait_for_vault_health(compose, os.environ)
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


def test_approle_rotation_recovers_after_rejected_logins():
    """Повторный запуск восстанавливает роли, заблокированные старыми ключами."""
    url = os.environ["TEST_VAULT_URL"]
    client = hvac.Client(
        url=url,
        token=os.environ["TEST_VAULT_TOKEN"],
        verify=os.getenv("TEST_VAULT_CACERT") or True,
    )
    compose = json.loads(os.environ["TEST_COMPOSE_COMMAND"])
    services = ["diabetes-api", "kafka-consumer", "metrics-exporter"]
    subprocess.run(compose + ["stop", *services], check=True)
    database = client.secrets.kv.v2.read_secret_version(
        path="database/postgres", raise_on_deleted_version=True
    )["data"]["data"]
    for prefix in ("API", "CONSUMER"):
        service = hvac.Client(url=url, verify=os.getenv("TEST_VAULT_CACERT") or True)
        for _ in range(6):
            with pytest.raises(hvac.exceptions.VaultError):
                service.auth.approle.login(
                    role_id=os.environ[f"{prefix}_VAULT_ROLE_ID"],
                    secret_id="incorrect-test-secret",
                )
        # Блокировка отклоняет даже действующий ключ, а не только неверный.
        with pytest.raises(hvac.exceptions.Forbidden):
            service.auth.approle.login(
                role_id=os.environ[f"{prefix}_VAULT_ROLE_ID"],
                secret_id=os.environ[f"{prefix}_VAULT_SECRET_ID"],
            )
    restored_database, identities = configure_vault(
        client, {}, restricted_database=os.getenv("TEST_RESTRICTED_DATABASE") == "1"
    )
    assert restored_database == database
    for prefix in ("API", "CONSUMER"):
        service = hvac.Client(url=url, verify=os.getenv("TEST_VAULT_CACERT") or True)
        service.auth.approle.login(
            role_id=identities[f"{prefix}_VAULT_ROLE_ID"],
            secret_id=identities[f"{prefix}_VAULT_SECRET_ID"],
        )
        assert (
            service.secrets.kv.v2.read_secret_version(
                path=f"database/{prefix.lower()}"
                if os.getenv("TEST_RESTRICTED_DATABASE") == "1"
                else "database/postgres",
                raise_on_deleted_version=True,
            )["data"]["data"]["POSTGRES_DB"]
            == database["POSTGRES_DB"]
        )
        service.auth.token.revoke_self()
    env = dict(os.environ, **identities)
    if os.getenv("TEST_VAULT_FILE_SECRETS") == "1":
        from scripts.vault_identity import write_identities

        project = compose[compose.index("-p") + 1]
        assert project != "devops_project"
        info = json.loads(
            subprocess.check_output(
                ["docker", "inspect", f"{project}-diabetes-api-1"], text=True
            )
        )[0]
        write_identities(project, info["Image"], identities, replace=True)
        for key in identities:
            env.pop(key, None)
    subprocess.run(
        compose + ["up", "-d", "--no-build", "--no-deps", *services],
        env=env,
        check=True,
    )
    wait_until(
        lambda: requests.get(os.environ["TEST_API_URL"] + "/db/health", timeout=5).ok,
        "API не восстановился после обновления AppRole",
    )


def test_installed_vault_transport_and_private_identity_files(monkeypatch):
    from scripts import deploy_release
    from scripts.vault_connection import host_bind_source, installed_connection

    compose = json.loads(os.environ["TEST_COMPOSE_COMMAND"])
    project = compose[compose.index("-p") + 1]
    assert project != "devops_project"
    names = ["vault", "diabetes-api", "kafka-consumer", "metrics-exporter"]
    services = {
        name: json.loads(
            subprocess.check_output(
                ["docker", "inspect", f"{project}-{name}-1"], text=True
            )
        )[0]
        for name in names
    }
    connection = installed_connection(services)
    assert connection.url.startswith(os.environ["TEST_VAULT_URL"])
    assert deploy_release.vault_ready(services)
    if os.getenv("TEST_VAULT_FILE_SECRETS") != "1":
        return
    monkeypatch.setattr(deploy_release, "PROJECT", project)
    ca = next(
        m["Source"]
        for m in services["diabetes-api"]["Mounts"]
        if m["Destination"] == "/run/vault/ca.crt"
    )
    compose_env = dict(
        os.environ, COMPOSE_DISABLE_ENV_FILE="1", VAULT_CA_FILE=host_bind_source(ca)
    )
    config = json.loads(
        subprocess.check_output(
            compose + ["config", "--format", "json"], env=compose_env, text=True
        )
    )
    runtime = deploy_release.configure_runtime(config, Path.cwd(), services)
    subprocess.run(
        ["docker", "compose", "-p", project, "-f", "-", "config", "--quiet"],
        input=json.dumps(runtime),
        env=compose_env,
        text=True,
        check=True,
    )
    for name in names[1:]:
        env = dict(
            item.split("=", 1)
            for item in services[name]["Config"]["Env"]
            if "=" in item
        )
        assert not env.get("VAULT_ROLE_ID") and not env.get("VAULT_SECRET_ID")
        rendered = runtime["services"][name]
        assert "VAULT_SECRET_ID" not in rendered["environment"]
        assert (
            rendered["environment"]["VAULT_SECRET_ID_FILE"]
            == "/run/vault-identity/secret_id"
        )
        mounts = [
            m
            for m in services[name]["Mounts"]
            if m["Destination"] == "/run/vault-identity"
        ]
        assert len(mounts) == 1 and not mounts[0]["RW"]
        probe = "from pathlib import Path; import stat; p=Path('/run/vault-identity/secret_id'); s=p.stat(); assert s.st_uid==10001 and stat.S_IMODE(s.st_mode)==0o400; assert p.read_text().strip()"
        subprocess.run(
            ["docker", "exec", services[name]["Id"], "python", "-c", probe], check=True
        )
        denied = subprocess.run(
            [
                "docker",
                "exec",
                "--user",
                "65534:65534",
                services[name]["Id"],
                "python",
                "-c",
                "from pathlib import Path; Path('/run/vault-identity/secret_id').read_text()",
            ],
            capture_output=True,
        )
        assert denied.returncode != 0
