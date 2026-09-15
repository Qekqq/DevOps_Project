import json
import sys
from unittest.mock import MagicMock

import pytest
from cryptography.exceptions import InvalidTag

from scripts import deploy_release as deploy
from scripts import security_migration as migration
from scripts.start_stack import validate_service_database


def test_cli_refuses_pending_migration_before_asking_for_password(
    tmp_path, monkeypatch
):
    (tmp_path / migration.PENDING).write_text("{}")
    monkeypatch.setattr(deploy, "deployment_home", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["migration", "--apply", "--release", "candidate"])
    monkeypatch.setattr(
        migration, "get_bootstrap", lambda _: pytest.fail("Must not ask for password")
    )
    with pytest.raises(SystemExit, match="перенос"):
        migration.main()


def test_cli_routes_explicit_manual_recovery_under_the_lock(tmp_path, monkeypatch):
    (tmp_path / migration.PENDING).write_text("{}")
    monkeypatch.setattr(deploy, "deployment_home", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["migration", "--apply", "--restore"])
    monkeypatch.setattr(
        migration, "get_bootstrap", lambda _: {"unseal_key": "synthetic"}
    )
    restore = MagicMock()
    monkeypatch.setattr(migration, "restore", restore)
    migration.main()
    restore.assert_called_once_with(tmp_path, {"unseal_key": "synthetic"})


def test_migration_client_keeps_explicit_ca_and_ignores_proxy_environment():
    client = migration.client_for(
        "https://vault:8200", {"root_token": "synthetic"}, "trusted-ca.crt"
    )
    assert client.session.verify == "trusted-ca.crt"
    assert client.adapter._kwargs["verify"] == "trusted-ca.crt"
    assert not client.session.trust_env and not client.adapter.allow_redirects
    client.session.close()


def test_protocol_switch_retries_tls_handshake_without_falling_back_to_http(
    monkeypatch,
):
    client = MagicMock()
    client.sys.read_seal_status.side_effect = [
        migration.requests.exceptions.SSLError("WRONG_VERSION_NUMBER"),
        {"sealed": False},
    ]
    client.sys.is_initialized.return_value = True
    client.sys.is_sealed.return_value = False
    client.is_authenticated.return_value = True
    monkeypatch.setattr(
        migration.wait_until.__globals__["time"], "sleep", lambda _: None
    )
    migration.unseal(client, {"unseal_key": "synthetic"})
    assert client.sys.read_seal_status.call_count == 2
    client.sys.submit_unseal_key.assert_not_called()


def test_certificate_verification_failure_stops_without_sending_unseal_key():
    client = MagicMock()
    client.sys.read_seal_status.side_effect = migration.requests.exceptions.SSLError(
        "CERTIFICATE_VERIFY_FAILED"
    )
    with pytest.raises(RuntimeError, match="TLS verification"):
        migration.unseal(client, {"unseal_key": "synthetic"})
    assert client.sys.read_seal_status.call_count == 1
    client.sys.submit_unseal_key.assert_not_called()


@pytest.mark.parametrize(
    "key,value",
    [
        ("POSTGRES_USER", "administrator"),
        ("POSTGRES_HOST", "external"),
        ("POSTGRES_DB", "other"),
        ("POSTGRES_PASSWORD", "short"),
    ],
)
def test_drifted_service_secret_cannot_preserve_administrative_access(key, value):
    database = {
        "POSTGRES_HOST": "db",
        "POSTGRES_PORT": "5432",
        "POSTGRES_DB": "diabetes",
        "POSTGRES_USER": "administrator",
        "POSTGRES_PASSWORD": "a" * 64,
    }
    secret = dict(database, POSTGRES_USER="diabetes_api")
    validate_service_database(secret, database, "diabetes_api")
    secret[key] = value
    with pytest.raises(RuntimeError):
        validate_service_database(secret, database, "diabetes_api")


def test_recovery_secrets_are_authenticated_and_bound_to_attempt():
    secret = "synthetic-unseal-key-" + "a" * 32
    original = {"password": "PRIVATE-PASSWORD", "config": {"services": {}}}
    bundle = migration.seal_recovery(original, secret, "a" * 32)
    assert "PRIVATE-PASSWORD" not in json.dumps(bundle)
    assert migration.open_recovery(bundle, secret, "a" * 32) == original
    with pytest.raises(InvalidTag):
        migration.open_recovery(bundle, secret + "wrong", "a" * 32)
    with pytest.raises(ValueError):
        migration.open_recovery(bundle, secret, "b" * 32)
    other = migration.seal_recovery(original, secret, "a" * 32)
    assert other["payload"] != bundle["payload"]


def test_pending_migration_blocks_start_and_cd_until_manual_recovery(tmp_path):
    (tmp_path / migration.PENDING).write_text("{}")
    with pytest.raises(RuntimeError, match="перенос"):
        with deploy.deployment_lock(tmp_path):
            pytest.fail("Pending migration must prevent deployment")
    with deploy.deployment_lock(tmp_path, allow_pending_migration=True):
        pass


def test_recovery_passes_literal_dollars_on_stdin(monkeypatch):
    calls = []
    monkeypatch.setattr(migration, "command", lambda *a, **kw: calls.append((a, kw)))
    migration.run_config(
        {"services": {"db": {"environment": {"PASSWORD": "p$PRIVATE${SECRET}"}}}},
        "config",
        "--quiet",
    )
    args, kwargs = calls[0]
    assert "PRIVATE" not in str(args)
    assert (
        json.loads(kwargs["payload"])["services"]["db"]["environment"]["PASSWORD"]
        == "p$$PRIVATE$${SECRET}"
    )


def test_recovery_preserves_portable_docker_socket_instead_of_desktop_internal_path():
    config = {
        "services": {
            "docker-proxy": {
                "volumes": [
                    {
                        "type": "bind",
                        "source": "/var/run/docker.sock",
                        "target": "/var/run/docker.sock",
                    }
                ]
            }
        }
    }
    containers = {
        "docker-proxy": {
            "Image": "sha256:" + "a" * 64,
            "Config": {"Env": []},
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": "/run/host-services/docker.proxy.sock",
                    "Destination": "/var/run/docker.sock",
                    "RW": False,
                }
            ],
        }
    }
    result = migration.mounted_config(config, containers)
    assert (
        result["services"]["docker-proxy"]["volumes"][0]["source"]
        == "/var/run/docker.sock"
    )


def test_following_cd_preserves_migrated_volume_names(monkeypatch, tmp_path):
    config = {
        "services": {
            "diabetes-api": {
                "volumes": [
                    {
                        "target": "/app/data/feedback",
                        "source": "./data/feedback",
                        "type": "bind",
                    }
                ]
            }
        },
        "volumes": {},
    }
    services = {
        "diabetes-api": {
            "Mounts": [{"Destination": "/app/data/feedback", "Source": str(tmp_path)}]
        }
    }
    for name, target in migration.DATA_PATHS.items():
        config["services"][name] = {
            "volumes": [{"type": "volume", "source": "old", "target": target}]
        }
        services[name] = {
            "Config": {"Env": []},
            "Mounts": [
                {"Type": "volume", "Name": "new_" + name, "Destination": target}
            ],
        }
    monkeypatch.setattr(deploy, "preserve_transport", lambda *a: None)
    monkeypatch.setattr(deploy, "preserve_identities", lambda *a: None)
    result = deploy.configure_runtime(config, tmp_path, services)
    for name, logical in migration.SERVICE_VOLUMES.items():
        assert result["volumes"][logical] == {"name": "new_" + name, "external": True}


def test_wrong_recovery_key_cannot_stop_containers(tmp_path, monkeypatch):
    attempt = "a" * 32
    (tmp_path / migration.PENDING).write_text(json.dumps({"migration_id": attempt}))
    directory = tmp_path / "security-migrations" / attempt
    directory.mkdir(parents=True)
    bundle = migration.seal_recovery({}, "synthetic-key-" + "a" * 40, attempt)
    (directory / "recovery.enc.json").write_text(json.dumps(bundle))
    monkeypatch.setattr(
        migration, "command", lambda *a, **kw: pytest.fail("Docker must not be invoked")
    )
    with pytest.raises(InvalidTag):
        migration.restore(tmp_path, {"unseal_key": "wrong-key-" + "b" * 40})
