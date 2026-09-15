from unittest.mock import MagicMock

import pytest

from scripts.start_stack import require_empty_project


@pytest.mark.parametrize(
    "containers,volumes", [("stopped-id\n", ""), ("", "test_vault_data\n")]
)
def test_ci_refuses_existing_containers_or_vault_volume(
    monkeypatch, containers, volumes
):
    execute = MagicMock(side_effect=[containers, volumes])
    monkeypatch.setattr("scripts.start_stack.subprocess.check_output", execute)
    with pytest.raises(RuntimeError, match="--ci"):
        require_empty_project("test")
    assert all(
        call.args[0][:2] in (["docker", "ps"], ["docker", "volume"])
        for call in execute.call_args_list
    )


def test_ci_allows_empty_project_alongside_other_projects(monkeypatch):
    monkeypatch.setattr(
        "scripts.start_stack.subprocess.check_output",
        MagicMock(side_effect=["", "other_vault_data\n"]),
    )
    require_empty_project("test")
