"""Security boundaries: admission and diagnostic data minimization."""

import json
import logging
import sys

import pytest

from scripts import fetch_models
from scripts.model_delivery import inventory, stage_models
from src.request_limits import LoginLimits
from src.runtime_logging import DiagnosticFormatter
from src.secrets import vault_client


def test_limits_isolate_accounts_expire_and_bound_memory():
    limiter = LoginLimits(per_identity=2, total=4, capacity=2)
    assert limiter.admit("a", now=1)
    assert limiter.admit("a", now=2)
    assert not limiter.admit("a", now=3)
    assert limiter.admit("b", now=3)
    assert not limiter.admit("c", now=4)
    assert limiter.admit("b", now=4)
    assert not limiter.admit("b", now=5)
    assert limiter.admit("c", now=65)
    assert len(limiter.identities) == 1


def test_server_diagnostics_drop_messages_arguments_and_tracebacks():
    try:
        raise ValueError("patient=PAT123 password=secret-token glucose=150")
    except ValueError:
        record = logging.LogRecord(
            "uvicorn.error",
            logging.ERROR,
            "",
            0,
            "URL /studies/PAT123?token=%s",
            ("secret-token",),
            sys.exc_info(),
        )
    result = DiagnosticFormatter().format(record)
    assert json.loads(result)["error_type"] == "ValueError"
    assert all(
        value not in result for value in ("PAT123", "secret-token", "150", "URL")
    )


def test_model_delivery_rejects_substitution_and_does_not_replace_existing_release(
    tmp_path,
):
    source = tmp_path / "download"
    source.mkdir()
    (source / "current.json").write_text("{}")
    (source / "model.joblib").write_bytes(b"verified-model")
    manifest = {"model_files": inventory(source)}
    target = tmp_path / "installed"
    stage_models(manifest, source, target)
    assert inventory(target) == manifest["model_files"]
    (source / "model.joblib").write_bytes(b"substituted-model")
    with pytest.raises(ValueError, match="differ"):
        stage_models(manifest, source, target)
    assert (target / "model.joblib").read_bytes() == b"verified-model"
    with pytest.raises(ValueError, match="Invalid model path"):
        stage_models(
            {"model_files": {"current.json": "", "../outside": ""}}, source, target
        )


def test_secret_file_takes_precedence_and_cannot_silently_fall_back(
    tmp_path, monkeypatch
):
    path = tmp_path / "identity"
    path.write_text("file-secret")
    monkeypatch.setenv("VAULT_SECRET_ID_FILE", str(path))
    monkeypatch.setenv("VAULT_SECRET_ID", "old-secret")
    assert vault_client.get_required_env("VAULT_SECRET_ID") == "file-secret"
    path.unlink()
    with pytest.raises(vault_client.VaultSecretError):
        vault_client.get_required_env("VAULT_SECRET_ID")


def test_failed_model_download_restores_config_and_does_not_log_credentials(
    tmp_path, monkeypatch, capsys
):
    from types import SimpleNamespace

    (tmp_path / ".dvc").mkdir()
    config = tmp_path / ".dvc/config.local"
    previous = b"previous-local-configuration"
    config.write_bytes(previous)
    monkeypatch.setattr(fetch_models, "ROOT", tmp_path)
    monkeypatch.setenv("DVC_YANDEX_USER", "account")
    monkeypatch.setenv("DVC_YANDEX_PASSWORD", "private-password")
    monkeypatch.setattr(
        fetch_models.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=1, stderr=b"private-password"),
    )
    with pytest.raises(SystemExit) as error:
        fetch_models.main()
    assert config.read_bytes() == previous
    assert "private-password" not in str(error.value) + capsys.readouterr().out
