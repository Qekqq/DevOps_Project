import json

import pytest
from cryptography.exceptions import InvalidTag

from scripts.bootstrap_vps import open_keys, render_runtime, seal_keys


def test_recovery_requires_key_and_correct_installation():
    key = "ab" * 32
    values = {"root_token": "private-root", "unseal_key": "private-unseal"}
    content = seal_keys(values, key, "devops_project")
    assert b"private-root" not in content
    assert b"private-unseal" not in content
    assert open_keys(content, key, "devops_project") == values
    with pytest.raises(InvalidTag):
        open_keys(content, "cd" * 32, "devops_project")
    with pytest.raises(ValueError):
        open_keys(content, key, "another-project")
    tampered = json.loads(content)
    altered = bytearray.fromhex(tampered["ciphertext"])
    altered[0] ^= 1
    tampered["ciphertext"] = altered.hex()
    with pytest.raises(InvalidTag):
        open_keys(json.dumps(tampered), key, "devops_project")


@pytest.fixture
def config():
    names = (
        "db",
        "vault",
        "frontend",
        "diabetes-api",
        "kafka-consumer",
        "metrics-exporter",
        "grafana",
        "alloy",
        "docker-stats",
        "kafka",
    )
    services = {
        n: {"environment": {}, "volumes": [], "ports": ["0.0.0.0:9999:9999"]}
        for n in names
    }
    services["vault"].update(healthcheck={"test": ["CMD", "vault", "status"]})
    services["vault"]["volumes"] = [
        {"type": "volume", "source": "vault_data", "target": "/vault/file"},
        {
            "type": "bind",
            "source": "./vault/server.hcl",
            "target": "/vault/config/server.hcl",
        },
    ]
    services["db"]["volumes"] = [
        {"type": "volume", "source": "pgdata", "target": "/var/lib/postgresql/data"},
        {"type": "bind", "source": "./db", "target": "/docker-entrypoint-initdb.d"},
    ]
    services["diabetes-api"]["volumes"] = [
        {"type": "bind", "source": "./data/feedback", "target": "/app/data/feedback"}
    ]
    return {
        "services": services,
        "volumes": {
            "pgdata": {"name": "devops_project_pgdata"},
            "vault_data": {"name": "devops_project_vault_data"},
        },
        "networks": {"default": {"name": "devops_project_default"}},
    }


def test_new_installation_cannot_reuse_source_storage_or_public_ports(config):
    before = json.dumps(config, sort_keys=True)
    result = render_runtime(
        config, "/release", "/state", "test_target", "https://example.com"
    )
    assert json.dumps(config, sort_keys=True) == before
    for kind in ("volumes", "networks"):
        assert all(
            value["name"].startswith("test_target_") for value in result[kind].values()
        )
    services = result["services"]
    assert services["frontend"]["ports"] == ["127.0.0.1:8080:8080"]
    assert services["vault"]["ports"] == ["127.0.0.1:8201:8200"]
    assert all(
        "ports" not in service
        for name, service in services.items()
        if name not in ("frontend", "vault")
    )
    assert all(m["type"] == "volume" for m in services["db"]["volumes"])
    for name in ("diabetes-api", "kafka-consumer", "metrics-exporter"):
        env = services[name]["environment"]
        assert env["VAULT_ADDR"] == "https://vault:8200"
        assert env["VAULT_SECRET_ID"] == ""
        assert env["VAULT_SECRET_ID_FILE"].startswith("/run/vault-identity/")
    assert services["diabetes-api"]["environment"]["SESSION_COOKIE_SECURE"] == "true"


def test_public_origin_validation(config):
    for origin in (
        "http://example.com",
        "https://user:password@example.com",
        "https://example.com/path",
        "https://example.com?q=1",
    ):
        with pytest.raises(ValueError):
            render_runtime(config, "/release", "/state", "devops_project", origin)


def test_transport_writes_verified_bytes_without_overwriting(tmp_path):
    from scripts.bootstrap_transport import Transport

    target = tmp_path / "data.bin"
    transport = Transport()
    transport.write(target, b"\x00\xff\r\n")
    assert target.read_bytes() == b"\x00\xff\r\n"
    with pytest.raises(RuntimeError):
        transport.write(target, b"replacement")
    assert target.read_bytes() == b"\x00\xff\r\n"


def test_transport_hides_private_child_diagnostics():
    import sys

    from scripts.bootstrap_transport import Transport

    with pytest.raises(RuntimeError) as caught:
        Transport().run(
            [
                sys.executable,
                "-c",
                "import sys; print('secret-value',file=sys.stderr); sys.exit(2)",
            ]
        )
    assert "secret-value" not in str(caught.value)


def test_partial_source_stop_failure_restarts_original_writers(tmp_path, monkeypatch):
    from scripts import vps_data

    names = ("frontend", "diabetes-api", "kafka-consumer", "metrics-exporter", "kafka")
    services = {
        name: {"Id": name + "-original", "State": {"Running": True}} for name in names
    }
    calls = []

    def docker(*args):
        calls.append(args)
        if args[0] == "exec":
            return json.dumps(
                {
                    "KAFKA_PREDICTION_TOPIC": "predictions",
                    "KAFKA_CONSUMER_GROUP": "consumer",
                }
            )
        if args[0] == "stop":
            raise RuntimeError("stop interrupted")
        return ""

    monkeypatch.setattr(vps_data, "existing_services", lambda: services)
    monkeypatch.setattr(vps_data, "docker", docker)
    with pytest.raises(RuntimeError, match="stop interrupted"):
        vps_data.freeze_export(tmp_path / "snapshot")
    restart = next(args for args in calls if args[0] == "start")
    assert set(restart[1:]) == {name + "-original" for name in names if name != "kafka"}
    assert not list((tmp_path / "snapshot").glob("*.dump"))
