"""SSH delivery boundaries; no real network or production containers."""

import hashlib
import json
import shlex
import subprocess
import sys
import tarfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts import deploy_vps
from scripts.model_delivery import inventory

SHA = "a" * 40


@pytest.fixture
def package(tmp_path):
    root = tmp_path / "repo"
    release = root / "release"
    release.mkdir(parents=True)
    (root / "models").mkdir()
    (root / "models/current.json").write_text("{}")
    (root / "models/model.joblib").write_bytes(b"private-model")
    (root / "scripts").mkdir()
    for name in deploy_vps.DEPLOY_SCRIPTS:
        (root / "scripts" / name).write_text("pass")
    (root / "scripts/seed_demo_history.py").write_text("not runtime")
    (root / ".env").write_text("PRIVATE=not-for-upload")
    (release / "unlisted-secret").write_text("not-for-upload")
    content = b'{"services":{"api":{"image":"example/api@sha256:' + b"b" * 64 + b'"}}}'
    (release / "docker-compose.json").write_bytes(content)
    manifest = {
        "commit": SHA,
        "ci_run_id": "123",
        "files": {"docker-compose.json": hashlib.sha256(content).hexdigest()},
        "model_files": inventory(root / "models"),
    }
    (release / "release.json").write_text(json.dumps(manifest))
    return root, release


@pytest.fixture
def environment():
    return {
        "VPS_HOST": "192.0.2.1",
        "VPS_USER": "deploy",
        "VPS_SSH_KEY": "-----BEGIN OPENSSH PRIVATE KEY-----\nprivate-test-key",
        "VPS_SSH_KNOWN_HOSTS": "192.0.2.1 ssh-ed25519 test-public-key",
        "DOCKERHUB_USERNAME": "test-user",
        "DOCKERHUB_READ_TOKEN": "private-test-token",
    }


def test_bundle_only_contains_verified_files_and_readable_mounts(package, tmp_path):
    root, release = package
    archive = tmp_path / "bundle.tar.gz"
    deploy_vps.build_bundle(archive, release, SHA, "123", root=root)
    with tarfile.open(archive) as stream:
        assert {m.name for m in stream if m.isfile()} == {
            *("scripts/" + name for name in deploy_vps.DEPLOY_SCRIPTS),
            "release/release.json",
            "release/docker-compose.json",
            "models/current.json",
            "models/model.joblib",
        }
        assert stream.getmember("models").mode == 0o755
        assert stream.getmember("models/model.joblib").mode == 0o644
        assert all(m.isdir() or m.isfile() for m in stream)


@pytest.mark.parametrize(
    "target", ["models/model.joblib", "release/docker-compose.json"]
)
def test_changed_verified_inputs_stop_before_delivery(package, tmp_path, target):
    root, release = package
    (root / target).write_bytes(b"changed")
    with pytest.raises(ValueError):
        deploy_vps.build_bundle(
            tmp_path / "bundle.tar.gz", release, SHA, "123", root=root
        )


def test_bundle_rejects_traversal_even_when_resolving_inside_release(package, tmp_path):
    root, release = package
    (release / "sub").mkdir()
    manifest = json.loads((release / "release.json").read_text())
    manifest["files"]["sub/../docker-compose.json"] = manifest["files"][
        "docker-compose.json"
    ]
    (release / "release.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="bundle path"):
        deploy_vps.build_bundle(
            tmp_path / "bundle.tar.gz", release, SHA, "123", root=root
        )


def test_failed_preflight_never_uploads_or_changes_server(
    package, environment, monkeypatch
):
    root, release = package
    run = MagicMock(return_value=subprocess.CompletedProcess([], 1))
    monkeypatch.setattr(deploy_vps.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="not bootstrapped"):
        deploy_vps.deploy(release, SHA, "123", root=root, environment=environment)
    assert run.call_count == 1
    assert "current.json" in run.call_args.args[0][-1]
    assert "mkdir" not in run.call_args.args[0][-1]


def test_secret_delivery_uses_stdin_and_pinned_host_key(
    package, environment, monkeypatch
):
    root, release = package
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(deploy_vps.subprocess, "run", run)
    deploy_vps.deploy(release, SHA, "123", root=root, environment=environment)
    assert run.call_count == 3
    for call in run.call_args_list:
        args = call.args[0]
        assert "StrictHostKeyChecking=yes" in args
        assert "BatchMode=yes" in args
        assert "private-test-token" not in str(args)
        assert "private-test-key" not in str(args)
        assert not deploy_vps.PRIVATE_ENV.intersection(call.kwargs["env"])
    assert run.call_args.kwargs["input"] == "private-test-token\n"
    key = Path(run.call_args.args[0][run.call_args.args[0].index("-i") + 1])
    assert not key.exists()
    script = run.call_args.args[0][-1]
    assert "trap cleanup_auth EXIT" in script
    assert "pip install" not in script
    assert "--preflight" in script


@pytest.mark.parametrize(
    "field,value", [("VPS_HOST", "host;id"), ("VPS_USER", "root;id")]
)
def test_connection_rejects_shell_fragments(tmp_path, environment, field, value):
    environment[field] = value
    with pytest.raises(ValueError):
        deploy_vps.connection(tmp_path, environment)


def test_remote_username_is_shell_quoted():
    username = "user; echo injected"
    script = deploy_vps.remote_script(
        "/opt/devops_project/incoming/test", SHA, "123", username
    )
    login = next(
        line for line in script.splitlines() if line.startswith("docker login")
    )
    assert shlex.split(login) == [
        "docker",
        "login",
        "--username",
        username,
        "--password-stdin",
    ]


def test_deployment_requires_only_python_standard_library():
    subprocess.run(
        [sys.executable, "-S", "-c", "import scripts.deploy_vps"], check=True
    )


def test_selected_scripts_import_without_repository_or_third_party_packages(tmp_path):
    import shutil

    destination = tmp_path / "scripts"
    destination.mkdir()
    for name in deploy_vps.DEPLOY_SCRIPTS:
        shutil.copyfile(deploy_vps.ROOT / "scripts" / name, destination / name)
    subprocess.run(
        [sys.executable, "-S", "-c", "import scripts.deploy_release"],
        cwd=tmp_path,
        check=True,
    )
