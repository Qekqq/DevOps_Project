import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from scripts import create_user


def test_user_creation_uses_admin_maintenance_stdin_not_api_credentials(monkeypatch):
    api = {
        "Id": "api-id",
        "Image": "sha256:api",
        "Config": {
            "Labels": {
                "com.docker.compose.project": "review",
                "com.docker.compose.service": "diabetes-api",
            }
        },
    }
    database = {
        "Config": {
            "Labels": {
                "com.docker.compose.project": "review",
                "com.docker.compose.service": "db",
            },
            "Env": [
                "POSTGRES_USER=admin",
                "POSTGRES_DB=app",
                "POSTGRES_PASSWORD=private-value",
            ],
        }
    }
    monkeypatch.setattr(
        create_user.subprocess,
        "check_output",
        MagicMock(side_effect=[json.dumps([api]), json.dumps([database])]),
    )
    run = MagicMock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(create_user.subprocess, "run", run)
    create_user.provision_user(
        "review-diabetes-api-1",
        {"username": "new-user", "password_hash": "hash", "role": "admin"},
    )
    command = run.call_args.args[0]
    assert "create-user" in command and "container:api-id" in command
    assert "private-value" not in str(command) and "hash" not in str(command)
    payload = json.loads(run.call_args.kwargs["input"])
    assert payload["database"]["POSTGRES_USER"] == "admin"
    assert payload["user"]["role"] == "admin"


def test_unrelated_container_is_rejected_before_user_creation(monkeypatch):
    monkeypatch.setattr(
        create_user.subprocess,
        "check_output",
        lambda _: json.dumps([{"Config": {"Labels": {}}}]),
    )
    run = MagicMock()
    monkeypatch.setattr(create_user.subprocess, "run", run)
    with pytest.raises(ValueError):
        create_user.provision_user("unrelated", {})
    run.assert_not_called()
