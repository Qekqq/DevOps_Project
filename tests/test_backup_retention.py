import json

from scripts.backup_database import prune_release_backups


def dumps(home, count=4):
    folder = home / "backups"
    folder.mkdir()
    result = []
    for day in range(1, count + 1):
        path = folder / f"postgres-202609{day:02d}T120000000000Z.dump"
        path.write_bytes(b"PGDMP-test")
        result.append(path)
    return result


def test_only_two_latest_completed_dumps_remain(tmp_path):
    paths = dumps(tmp_path)
    incomplete = tmp_path / "backups/postgres-20260801T120000000000Z.dump.partial"
    incomplete.write_bytes(b"partial")
    foreign = tmp_path / "backups/important.dump"
    foreign.write_bytes(b"PGDMP-user-backup")
    assert prune_release_backups(tmp_path) == 2
    assert [p.exists() for p in paths] == [False, False, True, True]
    assert incomplete.exists() and foreign.exists()


def test_current_and_migration_recovery_backups_are_preserved(tmp_path):
    paths = dumps(tmp_path)
    (tmp_path / "current.json").write_text(
        json.dumps({"database_backup": str(paths[0])})
    )
    journal = tmp_path / "security-migrations/id/journal.json"
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({"database_backup": str(paths[1])}))
    assert prune_release_backups(tmp_path) == 0
    assert all(p.exists() for p in paths)


def test_pending_migration_disables_cleanup(tmp_path):
    paths = dumps(tmp_path)
    (tmp_path / "security-migration.pending.json").write_text("{}")
    assert prune_release_backups(tmp_path) == 0
    assert all(p.exists() for p in paths)


def test_malformed_recovery_journal_prevents_deletion(tmp_path):
    import pytest

    paths = dumps(tmp_path)
    journal = tmp_path / "security-migrations/id/journal.json"
    journal.parent.mkdir(parents=True)
    journal.write_text("broken")
    with pytest.raises(ValueError):
        prune_release_backups(tmp_path)
    assert all(p.exists() for p in paths)


def test_non_archives_and_directories_are_not_deleted(tmp_path):
    paths = dumps(tmp_path)
    paths[0].write_bytes(b"not a backup")
    directory = tmp_path / "backups/postgres-20260101T120000000000Z.dump"
    directory.mkdir()
    assert prune_release_backups(tmp_path) == 1
    assert paths[0].exists() and directory.exists()
