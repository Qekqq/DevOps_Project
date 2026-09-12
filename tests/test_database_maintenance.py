import json
from unittest.mock import Mock

import pytest

from scripts.database_maintenance import execute
from scripts.maintenance_client import run_maintenance
from scripts.provision_runtime_users import provision


def test_maintenance_transports_password_only_on_private_stdin(monkeypatch):
    run = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr("scripts.maintenance_client.subprocess.run", run)
    database = {"POSTGRES_PASSWORD": "private-password"}
    run_maintenance(["docker", "compose"], {}, database, ["scripts.update_database"])
    args, kwargs = run.call_args
    assert "private-password" not in str(args)
    assert not kwargs["env"]
    assert json.loads(kwargs["input"])["database"] == database
    assert kwargs["capture_output"] and "-T" in args[0]


def test_maintenance_failure_does_not_disclose_private_output(monkeypatch):
    monkeypatch.setattr(
        "scripts.maintenance_client.subprocess.run",
        Mock(return_value=Mock(returncode=1, stderr="private-password")),
    )
    with pytest.raises(RuntimeError) as error:
        run_maintenance(["docker", "compose"], {}, {}, ["scripts.update_database"])
    assert "private-password" not in str(error.value)


@pytest.mark.parametrize("command", [[], ["os"], ["integration-check"]])
def test_maintenance_rejects_unapproved_command_before_connecting(command, monkeypatch):
    monkeypatch.delenv("RUN_INTEGRATION_TESTS", raising=False)
    with pytest.raises(ValueError):
        execute({}, command)


def test_runtime_provision_rejects_arbitrary_role_name():
    with pytest.raises(ValueError):
        provision({"postgres": "x" * 64})
