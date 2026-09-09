"""Успешный архив отличается от незавершённой или невалидной копии."""

import subprocess
from unittest.mock import MagicMock

import pytest

from scripts import backup_database


def test_backup_streams_binary_data_and_publishes_after_catalog_check(
    tmp_path, monkeypatch
):
    content = b"PGDMP\x00\xff\r\n"

    def run(command, **kwargs):
        assert command[:4] == ["docker", "exec", "-i", "database-id"]
        assert kwargs["check"] is True
        assert not list(tmp_path.glob("*.dump"))
        if command[-1] == "--list":
            assert kwargs["stdin"].read() == content
        else:
            kwargs["stdout"].write(content)

    execute = MagicMock(side_effect=run)
    monkeypatch.setattr(backup_database.subprocess, "run", execute)
    path = backup_database.create_database_backup(tmp_path, container="database-id")
    assert path.read_bytes() == content
    assert not list(tmp_path.glob("*.partial"))
    assert execute.call_count == 2


@pytest.mark.parametrize("failure", ["dump", "empty", "catalog"])
def test_failed_backup_is_never_published_as_dump(tmp_path, monkeypatch, failure):
    def run(command, **kwargs):
        if command[-1] == "--list":
            raise subprocess.CalledProcessError(1, command)
        if failure == "dump":
            kwargs["stdout"].write(b"partial")
            raise subprocess.CalledProcessError(1, command)
        if failure == "catalog":
            kwargs["stdout"].write(b"invalid archive")

    monkeypatch.setattr(backup_database.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="обновление запрещено"):
        backup_database.create_database_backup(tmp_path, container="database-id")
    assert not list(tmp_path.glob("*.dump"))
    assert len(list(tmp_path.glob("*.partial"))) == 1
