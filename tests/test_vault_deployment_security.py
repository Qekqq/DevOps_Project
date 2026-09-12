import copy
import json
import ssl
import subprocess
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock

import pytest

from scripts.deploy_release import service_environment, wait_for_vault_response
from scripts.vault_connection import (
    CLIENTS,
    host_bind_source,
    installed_connection,
    preserve_transport,
)
from scripts.vault_identity import (
    SERVICES,
    TARGET,
    preserve_identities,
    write_identities,
)
from scripts.vault_tls import issue_identity


def containers(ca=None, port=8201):
    result = {
        name: {
            "Config": {
                "Env": ["VAULT_ADDR=" + ("https" if ca else "http") + "://vault:8200"]
            },
            "Mounts": [],
        }
        for name in CLIENTS
    }
    result["vault"] = {
        "HostConfig": {
            "PortBindings": {
                "8200/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(port)}]
            }
        }
    }
    if ca:
        for name in CLIENTS:
            result[name]["Config"]["Env"].append("VAULT_CACERT=/run/vault/ca.crt")
            result[name]["Mounts"].append(
                {
                    "Destination": "/run/vault/ca.crt",
                    "Source": str(ca),
                    "Type": "bind",
                    "RW": False,
                }
            )
    return result


@pytest.mark.parametrize("prefix", ["/run/desktop/mnt/host", "/host_mnt"])
def test_docker_desktop_bind_source_is_mapped_to_windows_drive(prefix):
    source = prefix + "/c/Users/example/My Project/ca.crt"
    assert (
        host_bind_source(source, windows=True) == "C:/Users/example/My Project/ca.crt"
    )
    assert host_bind_source(source, windows=False) == source
    assert (
        host_bind_source("/run/desktop/mnt/host/wsl/custom/ca.crt", windows=True)
        == "/run/desktop/mnt/host/wsl/custom/ca.crt"
    )


def file_identities(services):
    for name, prefix in SERVICES.items():
        services[name]["Config"]["Env"] += [
            f"VAULT_ROLE_ID_FILE={TARGET}/role_id",
            f"VAULT_SECRET_ID_FILE={TARGET}/secret_id",
        ]
        services[name]["Mounts"].append(
            {
                "Destination": TARGET,
                "Type": "volume",
                "Name": "example_" + prefix.lower() + "_vault_identity",
                "RW": False,
            }
        )


def test_installed_transport_uses_tls_and_rejects_wrong_ca(tmp_path, monkeypatch):
    identity = issue_identity()
    for name, value in identity.items():
        (tmp_path / name).write_text(value, encoding="ascii")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"sealed":false}')

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tmp_path / "server.crt", tmp_path / "server.key")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # A shell proxy setting must not receive local Vault requests.
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    try:
        services = containers(tmp_path / "ca.crt", server.server_port)
        connection = installed_connection(services)
        assert wait_for_vault_response(
            connection.url + "sys/seal-status", opener=connection.opener
        ) == {"sealed": False}
        (tmp_path / "ca.crt").write_text(issue_identity()["ca.crt"], encoding="ascii")
        wrong = installed_connection(services)
        with pytest.raises(RuntimeError, match="TLS certificate verification failed"):
            wait_for_vault_response(wrong.url + "sys/seal-status", opener=wrong.opener)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_local_vault_redirect_is_not_followed(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/untrusted")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    try:
        connection = installed_connection(containers(port=server.server_port))
        with pytest.raises(urllib.error.HTTPError) as error:
            connection.opener.open(connection.url, timeout=3)
        assert error.value.code == 302
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_inconsistent_or_insecure_tls_settings_are_refused(tmp_path):
    services = containers(tmp_path / "ca.crt")
    services["kafka-consumer"]["Config"]["Env"][0] = "VAULT_ADDR=http://vault:8200"
    with pytest.raises(RuntimeError, match="disagree"):
        installed_connection(services)
    services = containers(tmp_path / "ca.crt")
    services["diabetes-api"]["Mounts"][0]["RW"] = True
    with pytest.raises(RuntimeError, match="read-only"):
        installed_connection(services)
    services = containers()
    services["vault"]["HostConfig"]["PortBindings"]["8200/tcp"][0]["HostIp"] = "0.0.0.0"
    with pytest.raises(RuntimeError, match="loopback"):
        installed_connection(services)


def test_cd_preserves_ca_and_own_identity_without_serializing_secrets(
    tmp_path, monkeypatch
):
    services = containers(tmp_path / "ca.crt")
    file_identities(services)
    config = {
        "services": {
            name: {
                "environment": {"VAULT_SECRET_ID": "${API_VAULT_SECRET_ID}"},
                "volumes": [],
            }
            for name in CLIENTS
        }
    }
    preserve_transport(config, services)
    preserve_identities(config, services, "example")
    for name, prefix in SERVICES.items():
        rendered = config["services"][name]
        assert rendered["environment"]["VAULT_ADDR"] == "https://vault:8200"
        assert rendered["environment"]["VAULT_CACERT"] == "/run/vault/ca.crt"
        assert "VAULT_SECRET_ID" not in rendered["environment"]
        assert rendered["environment"]["VAULT_SECRET_ID_FILE"] == TARGET + "/secret_id"
        assert {m["target"] for m in rendered["volumes"]} == {
            TARGET,
            "/run/vault/ca.crt",
        }
        assert config["volumes"][prefix.lower() + "_vault_identity"]["external"]
    services["db"] = {
        "Config": {
            "Env": ["POSTGRES_DB=db", "POSTGRES_USER=owner", "POSTGRES_PASSWORD=test"]
        }
    }
    monkeypatch.setenv("API_VAULT_SECRET_ID", "accidental-shell-value")
    env = service_environment(services)
    assert "API_VAULT_SECRET_ID" not in env
    assert "accidental-shell-value" not in json.dumps(config)
    bad = copy.deepcopy(services)
    bad["diabetes-api"]["Mounts"][-1]["Name"] = "example_exporter_vault_identity"
    with pytest.raises(RuntimeError, match="own read-only"):
        preserve_identities(config, bad, "example")


def test_identity_writer_refuses_foreign_volume_before_writing(monkeypatch):
    execute = MagicMock(
        return_value=subprocess.CompletedProcess(
            [], 0, json.dumps([{"Labels": {"devops.vault-identity": "someone-else"}}])
        )
    )
    monkeypatch.setattr("scripts.vault_identity.subprocess.run", execute)
    identities = {
        f"{prefix}_VAULT_{suffix}": "private-test-value"
        for prefix in SERVICES.values()
        for suffix in ("ROLE_ID", "SECRET_ID")
    }
    with pytest.raises(RuntimeError, match="Refusing to overwrite"):
        write_identities("example", "test-image", identities, replace=True)
    execute.assert_called_once()
    assert "private-test-value" not in str(execute.call_args)
