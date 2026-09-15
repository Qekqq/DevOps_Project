from unittest.mock import MagicMock

import pytest
from requests import ConnectionError

from src.secrets import vault_client


def test_connection_error_is_translated_to_service_error(monkeypatch):
    for key in ("VAULT_ADDR", "VAULT_ROLE_ID", "VAULT_SECRET_ID"):
        monkeypatch.setenv(key, "test")
    client = MagicMock()
    client.auth.approle.login.side_effect = ConnectionError("unreachable")
    monkeypatch.setattr(vault_client.hvac, "Client", lambda **kwargs: client)
    with pytest.raises(vault_client.VaultSecretError):
        vault_client.get_vault_client()


def test_app_uses_approle_without_root_token(monkeypatch):
    monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
    monkeypatch.setenv("VAULT_ROLE_ID", "api-role")
    monkeypatch.setenv("VAULT_SECRET_ID", "api-secret")
    monkeypatch.setenv("VAULT_TOKEN", "must-not-be-used")
    factory = MagicMock()
    monkeypatch.setattr(vault_client.hvac, "Client", factory)
    client = vault_client.get_vault_client()
    factory.assert_called_once_with(url="http://vault:8200", timeout=10, verify=True)
    client.auth.approle.login.assert_called_once_with(
        role_id="api-role", secret_id="api-secret"
    )


def test_root_token_cannot_replace_service_identity(monkeypatch):
    monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
    monkeypatch.setenv("VAULT_TOKEN", "root")
    monkeypatch.delenv("VAULT_ROLE_ID", raising=False)
    monkeypatch.setattr(vault_client.hvac, "Client", MagicMock())
    with pytest.raises(vault_client.VaultSecretError, match="VAULT_ROLE_ID"):
        vault_client.get_vault_client()
