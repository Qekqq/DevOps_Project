"""Выкладка проверенных образов и запрет изменений при неготовом окружении."""

import hashlib
import json
import re
import shutil
import subprocess
from unittest.mock import MagicMock

import pytest

from scripts import deploy_release as deploy
from scripts.package_release import package_release

SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64


@pytest.fixture
def release(tmp_path):
    folder = tmp_path / "package"
    folder.mkdir()
    config = {
        "services": {
            name: {"image": f"example/{name}@{DIGEST}", "volumes": []}
            for name in deploy.APPLICATION + deploy.MONITORING + deploy.INFRASTRUCTURE
        }
    }
    config["services"]["diabetes-api"]["volumes"] = [
        {"type": "bind", "source": "./data/feedback", "target": "/app/data/feedback"}
    ]
    config["services"]["prometheus"]["volumes"] = [
        {
            "type": "bind",
            "source": "./monitoring/prometheus.yml",
            "target": "/etc/prometheus/prometheus.yml",
        }
    ]
    content = json.dumps(config).encode()
    (folder / "docker-compose.json").write_bytes(content)
    manifest = {
        "commit": SHA,
        "ci_run_id": "123",
        "files": {"docker-compose.json": hashlib.sha256(content).hexdigest()},
    }
    (folder / "release.json").write_text(json.dumps(manifest))
    return folder


@pytest.fixture
def services():
    result = {
        name: {"Id": f"{name}-container-id", "Image": name, "State": {"Running": True}}
        for name in deploy.INFRASTRUCTURE
    }
    for name in ("diabetes-api", "kafka-consumer"):
        result[name] = {
            "Config": {"Env": ["VAULT_ROLE_ID=role", "VAULT_SECRET_ID=private-value"]}
        }
    result["diabetes-api"]["Mounts"] = [
        {"Destination": "/app/data/feedback", "Source": "C:/existing-data/feedback"}
    ]
    result["db"]["Config"] = {
        "Env": [
            "POSTGRES_DB=diabetes",
            "POSTGRES_USER=owner",
            "POSTGRES_PASSWORD=db-password",
        ]
    }
    return result


def test_manifest_rejects_other_commit_or_ci_run_and_changed_files(release):
    deploy.read_release(release, SHA, "123")
    with pytest.raises(ValueError, match="коммит"):
        deploy.read_release(release, "c" * 40, "123")
    with pytest.raises(ValueError, match="запуск"):
        deploy.read_release(release, SHA, "124")
    (release / "docker-compose.json").write_text("{}")
    with pytest.raises(ValueError, match="Изменён"):
        deploy.read_release(release)


@pytest.mark.parametrize("patch", [{"image": "example/api:latest"}, {"build": "."}])
def test_manifest_never_allows_local_build_or_mutable_image(release, patch):
    manifest, config = deploy.read_release(release)
    config["services"]["diabetes-api"].update(patch)
    content = json.dumps(config).encode()
    (release / "docker-compose.json").write_bytes(content)
    manifest["files"]["docker-compose.json"] = hashlib.sha256(content).hexdigest()
    (release / "release.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="готовые образы"):
        deploy.read_release(release)


def test_runtime_uses_versioned_config_and_preserves_feedback_location(
    release, services
):
    _, config = deploy.read_release(release)
    runtime = deploy.configure_runtime(config, release, services)
    assert (
        runtime["services"]["diabetes-api"]["volumes"][0]["source"]
        == "C:/existing-data/feedback"
    )
    assert runtime["services"]["prometheus"]["volumes"][0]["source"] == str(
        release / "monitoring/prometheus.yml"
    )
    assert (
        config["services"]["diabetes-api"]["volumes"][0]["source"] == "./data/feedback"
    )
    assert "private-value" not in json.dumps(runtime)
    assert "db-password" not in json.dumps(runtime)


def test_existing_credentials_are_passed_only_in_environment(services, monkeypatch):
    monkeypatch.setenv("PUBLIC_ORIGIN", "https://accidental-local-change.example")
    env = deploy.service_environment(services)
    assert env["API_VAULT_SECRET_ID"] == "private-value"
    assert env["CONSUMER_VAULT_SECRET_ID"] == "private-value"
    assert env["POSTGRES_PASSWORD"] == "db-password"
    assert "PUBLIC_ORIGIN" not in env


@pytest.fixture
def deployment(release, services, tmp_path, monkeypatch):
    monkeypatch.setenv("DEVOPS_DEPLOY_HOME", str(tmp_path / "deployment"))
    monkeypatch.setattr(deploy, "existing_services", lambda: services)
    monkeypatch.setattr(deploy, "vault_ready", lambda: True)
    monkeypatch.setattr(
        deploy,
        "docker_json",
        lambda *args: [
            {"Id": args[-1], "RepoDigests": [f"example/{args[-1]}@{DIGEST}"]}
        ],
    )
    execute = MagicMock()
    health = MagicMock()
    monkeypatch.setattr(deploy, "compose", execute)
    monkeypatch.setattr(deploy, "check_application", health)
    monkeypatch.setattr(
        deploy,
        "create_database_backup",
        MagicMock(return_value=tmp_path / "backup.dump"),
    )
    return execute, health


def test_deploy_uses_no_build_preserves_infrastructure_and_records_success(
    release, deployment
):
    execute, health = deployment
    deploy.deploy(release, SHA, "123")
    commands = [call.args[2:] for call in execute.call_args_list]
    assert not any("build" in command or "down" in command for command in commands)
    updates = [command for command in commands if command[0] == "up"]
    assert len(updates) == 1
    assert "--no-build" in updates[0] and "--no-deps" in updates[0]
    assert not set(deploy.INFRASTRUCTURE).intersection(updates[0])
    health.assert_called_once()
    state = json.loads((deploy.deployment_home() / "current.json").read_text())
    assert state["commit"] == SHA
    for path in deploy.deployment_home().rglob("*.json"):
        assert "private-value" not in path.read_text()
        assert "db-password" not in path.read_text()


def test_deployment_commands_use_supported_compose_flags(release, deployment):
    if not shutil.which("docker"):
        pytest.skip("Docker CLI is required to verify Compose flags")
    execute, _ = deployment
    deploy.deploy(release, SHA, "123")
    help_by_command = {}
    for call in execute.call_args_list:
        command = call.args[2:]
        verb = command[0]
        if verb not in help_by_command:
            result = subprocess.run(
                ["docker", "compose", verb, "--help"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            )
            help_by_command[verb] = set(re.findall(r"(?<!\S)--?[\w-]+", result.stdout))
        # Arguments after the service name belong to Python, not Compose.
        options = (
            command[1 : command.index("diabetes-api")] if verb == "run" else command[1:]
        )
        for option in options:
            if option.startswith("-"):
                assert option in help_by_command[verb], (verb, option)
        if verb in ("run", "up"):
            assert command[command.index("--pull") + 1] == "never"


def test_failed_health_does_not_replace_last_successful_release(release, deployment):
    _, health = deployment
    home = deploy.deployment_home()
    home.mkdir()
    state = home / "current.json"
    state.write_text('{"commit":"previous"}')
    health.side_effect = RuntimeError("not ready")
    with pytest.raises(RuntimeError, match="not ready"):
        deploy.deploy(release, SHA, "123")
    assert json.loads(state.read_text()) == {"commit": "previous"}
    assert not (home / "deployment.lock").exists()


def test_sealed_vault_does_not_change_containers(release, deployment, monkeypatch):
    execute, _ = deployment
    monkeypatch.setattr(deploy, "vault_ready", lambda: False)
    with pytest.raises(RuntimeError, match="Vault заблокирован"):
        deploy.deploy(release, SHA, "123")
    execute.assert_not_called()


def test_preflight_does_not_start_or_pull_containers(release, deployment):
    execute, health = deployment
    deploy.deploy(release, SHA, "123", preflight=True)
    assert [call.args[2:] for call in execute.call_args_list] == [("config", "--quiet")]
    health.assert_not_called()
    deploy.create_database_backup.assert_not_called()
    assert not (deploy.deployment_home() / "current.json").exists()


@pytest.mark.parametrize("preflight", [False, True])
def test_infrastructure_mismatch_blocks_pull_backup_and_update(
    release, deployment, monkeypatch, preflight
):
    execute, health = deployment
    monkeypatch.setattr(
        deploy, "docker_json", lambda *args: [{"Id": "different", "RepoDigests": []}]
    )
    with pytest.raises(RuntimeError, match="Образ vault отличается"):
        deploy.deploy(release, SHA, "123", preflight=preflight)
    execute.assert_not_called()
    deploy.create_database_backup.assert_not_called()
    health.assert_not_called()


@pytest.mark.parametrize("status", ["stopped", "starting", "unhealthy"])
def test_unready_infrastructure_blocks_deployment(
    release, deployment, services, status
):
    execute, health = deployment
    services["db"]["State"] = {
        "Running": status != "stopped",
        "Health": {"Status": status},
    }
    with pytest.raises(RuntimeError, match="Сервис db"):
        deploy.deploy(release, SHA, "123")
    execute.assert_not_called()
    deploy.create_database_backup.assert_not_called()
    health.assert_not_called()


def test_infrastructure_accepts_multi_platform_digest(release, services, monkeypatch):
    _, config = deploy.read_release(release)
    monkeypatch.setattr(
        deploy,
        "docker_json",
        lambda *args: [
            {"Id": "platform-specific-id", "RepoDigests": [f"repo@{DIGEST}"]}
        ],
    )
    deploy.check_infrastructure(config, services)


def test_backup_precedes_every_database_change_and_container_update(
    release, deployment
):
    execute, _ = deployment
    events = []
    backup_path = deploy.deployment_home() / "backups" / "backup.dump"

    def backup(*args, **kwargs):
        events.append("backup")
        return backup_path

    deploy.create_database_backup.side_effect = backup
    execute.side_effect = lambda *args: events.append(args[2])
    deploy.deploy(release, SHA, "123")
    deploy.create_database_backup.assert_called_once_with(
        deploy.deployment_home() / "backups", container="db-container-id"
    )
    assert events.index("backup") < events.index("run") < events.index("up")
    state = json.loads((deploy.deployment_home() / "current.json").read_text())
    assert state["database_backup"] == str(backup_path)


def test_failed_backup_blocks_database_changes_and_container_update(
    release, deployment
):
    execute, health = deployment
    deploy.create_database_backup.side_effect = RuntimeError("backup failed")
    with pytest.raises(RuntimeError, match="backup failed"):
        deploy.deploy(release, SHA, "123")
    assert not any(call.args[2] in ("run", "up") for call in execute.call_args_list)
    health.assert_not_called()
    assert not (deploy.deployment_home() / "current.json").exists()


def test_deployment_lock_blocks_overlapping_manual_and_ci_update(tmp_path):
    with deploy.deployment_lock(tmp_path):
        with pytest.raises(RuntimeError, match="Другая выкладка"):
            with deploy.deployment_lock(tmp_path):
                pytest.fail("Overlapping deployment was allowed")
    assert not (tmp_path / "deployment.lock").exists()


@pytest.mark.parametrize("pinned", [False, True])
def test_package_pins_images_and_has_no_build_context(tmp_path, monkeypatch, pinned):
    from scripts import package_release as module

    root = tmp_path / "source"
    for name in ("monitoring", "vault", "db"):
        (root / name).mkdir(parents=True)
        (root / name / "config.txt").write_text("settings")
    monkeypatch.setattr(module, "ROOT", root)
    config = {
        "services": {
            "frontend": {"build": {"context": "frontend"}},
            "diabetes-api": {"build": {"context": "."}},
            "db": {"image": f"postgres:16@{DIGEST}" if pinned else "postgres:16"},
        }
    }
    output = MagicMock(
        side_effect=[json.dumps(config), json.dumps([f"postgres@{DIGEST}"])]
    )
    monkeypatch.setattr(module.subprocess, "check_output", output)
    monkeypatch.setattr(module.subprocess, "run", MagicMock())
    folder = tmp_path / "release"
    package_release(
        folder, SHA, "123", f"example/api@{DIGEST}", f"example/frontend@{DIGEST}"
    )
    manifest, actual = deploy.read_release(folder, SHA, "123")
    assert actual["services"]["db"]["image"] == (
        f"postgres:16@{DIGEST}" if pinned else f"postgres@{DIGEST}"
    )
    assert output.call_count == (1 if pinned else 2)
    assert len(manifest["files"]) == 4
