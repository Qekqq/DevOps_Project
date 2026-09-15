import hashlib
import json
import subprocess
import sys
import tarfile
from unittest.mock import MagicMock

import pytest

from scripts import security_migration_plan as planning
from scripts import volume_snapshot as snapshot


def run_archive_helper(program, backup, metadata, source=None):
    program = program.replace("'/backup'", repr(str(backup)))
    if source:
        program = program.replace("'/source'", repr(str(source)))
    return subprocess.run(
        [sys.executable, "-c", program],
        input=json.dumps(metadata),
        capture_output=True,
        text=True,
    )


def test_archive_corruption_is_rejected(tmp_path):
    source, backup = tmp_path / "source", tmp_path / "backup"
    source.mkdir()
    backup.mkdir()
    (source / "probe").write_bytes(b"synthetic data")
    result = run_archive_helper(snapshot.COPY, backup, {"format": 1}, source)
    assert result.returncode == 0, result.stderr
    metadata = json.loads(result.stdout)
    assert run_archive_helper(snapshot.PUBLISH, backup, metadata).returncode == 0
    assert run_archive_helper(snapshot.VERIFY, backup, metadata).returncode == 0
    archive = backup / "data.tar"
    archive.chmod(0o600)
    with archive.open("ab") as stream:
        stream.write(b"corruption")
    result = run_archive_helper(snapshot.VERIFY, backup, metadata)
    assert result.returncode != 0 and "checksum mismatch" in result.stderr


def test_snapshot_refuses_low_disk_space_before_writing_archive(tmp_path):
    source, backup = tmp_path / "source", tmp_path / "backup"
    source.mkdir()
    backup.mkdir()
    (source / "data").write_bytes(b"synthetic")
    program = snapshot.COPY.replace(
        "metadata=json.load(sys.stdin)",
        "from types import SimpleNamespace\nshutil.disk_usage=lambda path: SimpleNamespace(free=1)\nmetadata=json.load(sys.stdin)",
    )
    result = run_archive_helper(program, backup, {}, source)
    assert result.returncode != 0 and "Insufficient space" in result.stderr
    assert not list(backup.iterdir())


@pytest.mark.parametrize(
    "name,kind",
    [
        ("data/../escape", tarfile.REGTYPE),
        ("/absolute", tarfile.REGTYPE),
        ("data/link", tarfile.SYMTYPE),
    ],
)
def test_archive_validation_rejects_traversal_and_links_even_with_matching_hash(
    tmp_path, name, kind
):
    archive = tmp_path / "data.tar"
    with tarfile.open(archive, "w") as output:
        member = tarfile.TarInfo(name)
        member.type = kind
        member.linkname = "/outside" if kind == tarfile.SYMTYPE else ""
        output.addfile(member)
    metadata = {
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "entries": 1,
        "archive_bytes": archive.stat().st_size,
    }
    (tmp_path / "manifest.json").write_text(json.dumps(metadata))
    result = run_archive_helper(snapshot.VERIFY, tmp_path, metadata)
    assert result.returncode != 0 and "Unsafe snapshot" in result.stderr


def test_invalid_snapshot_is_rejected_before_creating_restore_volume(monkeypatch):
    monkeypatch.setattr(
        snapshot,
        "verify_snapshot",
        MagicMock(side_effect=snapshot.SnapshotError("checksum mismatch")),
    )
    docker = MagicMock()
    monkeypatch.setattr(snapshot, "docker", docker)
    with pytest.raises(snapshot.SnapshotError, match="checksum"):
        snapshot.restore_snapshot_to_new_volume({})
    docker.assert_not_called()


def volume_info(project="devops_project", logical="pgdata"):
    return {
        "Driver": "local",
        "Options": None,
        "Labels": {
            "com.docker.compose.project": project,
            "com.docker.compose.volume": logical,
        },
    }


def test_snapshot_refuses_running_writer_before_creating_backup(monkeypatch):
    container = {
        "Id": "test-container",
        "Mounts": [{"Name": "test-source", "RW": True}],
        "State": {"Running": True},
    }
    docker = MagicMock(
        side_effect=[
            json.dumps([volume_info()]),
            "test-container",
            json.dumps([container]),
        ]
    )
    monkeypatch.setattr(snapshot, "docker", docker)
    with pytest.raises(snapshot.SnapshotError, match="active writer"):
        snapshot.create_snapshot("devops_project", "db", "test-source", "image")
    assert all(
        call.args[0] not in {"run", "stop", "start"} for call in docker.call_args_list
    )
    assert not any(
        call.args[:2] == ("volume", "create") for call in docker.call_args_list
    )


@pytest.mark.parametrize(
    "info",
    [
        volume_info("another-project"),
        volume_info(logical="vault_data"),
        {**volume_info(), "Options": {"type": "nfs"}},
    ],
)
def test_snapshot_rejects_foreign_or_unsupported_source_before_mounting(
    monkeypatch, info
):
    docker = MagicMock(return_value=json.dumps([info]))
    monkeypatch.setattr(snapshot, "docker", docker)
    with pytest.raises(snapshot.SnapshotError):
        snapshot.create_snapshot("devops_project", "db", "test-source", "image")
    docker.assert_called_once_with("volume", "inspect", "test-source")


def test_interrupted_source_does_not_publish_ready_snapshot(monkeypatch):
    def docker(*args, **kwargs):
        if args[:2] == ("volume", "inspect"):
            return json.dumps([volume_info()])
        if args[:2] == ("image", "inspect"):
            return json.dumps([{"Id": "sha256:" + "a" * 64}])
        return ""

    monkeypatch.setattr(snapshot, "docker", docker)
    monkeypatch.setattr(
        snapshot,
        "stopped_writers",
        MagicMock(
            side_effect=[
                {"container": {"StartedAt": "old"}},
                {"container": {"StartedAt": "new"}},
            ]
        ),
    )
    copy = MagicMock(return_value='{"sha256":"test"}')
    monkeypatch.setattr(snapshot, "helper", copy)
    with pytest.raises(snapshot.SnapshotError, match="incomplete") as error:
        snapshot.create_snapshot("devops_project", "db", "test-source", "image")
    assert error.value.volume.startswith("devops_project_snapshot_db_")
    copy.assert_called_once()
    assert copy.call_args.args[2] == snapshot.COPY


def test_migration_plan_contains_inventory_but_no_credentials(monkeypatch):
    services = {}
    for name, target in planning.DATA_PATHS.items():
        services[name] = {
            "Id": name + "-id",
            "Image": "sha256:" + "a" * 64,
            "State": {"Running": True},
            "Config": {
                "Labels": {
                    "com.docker.compose.project": "devops_project",
                    "com.docker.compose.service": name,
                },
                "Env": ["POSTGRES_PASSWORD=never-store-this"],
            },
            "Mounts": [
                {
                    "Destination": target,
                    "Name": "devops_project_" + snapshot.SERVICE_VOLUMES[name],
                    "Type": "volume",
                    "RW": True,
                }
            ],
        }
    for name in ("diabetes-api", "kafka-consumer", "metrics-exporter"):
        services[name] = {
            "Id": name + "-id",
            "Image": "image",
            "Config": {
                "Env": [
                    "VAULT_ROLE_ID=private-role",
                    "VAULT_SECRET_ID=private-secret",
                    "VAULT_ADDR=http://vault:8200",
                ]
            },
        }
    monkeypatch.setattr(
        planning.deploy,
        "docker_json",
        lambda *args: [volume_info(logical=args[-1].removeprefix("devops_project_"))],
    )
    plan = planning.build_plan(services, {"commit": "a" * 40})
    assert not plan["ready_to_apply"]
    assert all(
        word not in json.dumps(plan)
        for word in ("private-role", "private-secret", "never-store-this")
    )
    assert not any(client["vault_tls"] for client in plan["clients"])
    services["db"]["Id"] = "replaced-container"
    changed = planning.build_plan(services, {"commit": "a" * 40})
    assert changed["fingerprint"] != plan["fingerprint"]
