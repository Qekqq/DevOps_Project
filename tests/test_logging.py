"""Ротация обычного журнала нескольких модулей и разделение сервисов."""

import json
import logging
from datetime import datetime
from uuid import uuid4

import pytest

import src.logger as logging_module


@pytest.fixture
def loggers(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(logging_module, "MAX_LOG_BYTES", 512)
    created = []

    def create(service):
        if service is None:
            monkeypatch.delenv("APP_SERVICE", raising=False)
        else:
            monkeypatch.setenv("APP_SERVICE", service)
        logger = logging_module.get_logger("audit_" + uuid4().hex)
        created.append(logger)
        return logger

    yield create
    handlers = {handler for logger in created for handler in logger.handlers}
    for logger in created:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
    for handler in handlers:
        handler.close()
    for path, handler in list(logging_module._file_handlers.items()):
        if handler in handlers:
            del logging_module._file_handlers[path]


def test_modules_continue_logging_after_repeated_rotation(tmp_path, loggers):
    first, second = loggers("api"), loggers("api")
    for index in range(40):
        record = logging.LogRecord("probe", logging.INFO, "", 0, "private", (), None)
        record.created = index
        (first if index % 2 == 0 else second).handle(record)
    files = sorted(tmp_path.iterdir())
    assert [path.name for path in files] == ["api.log", "api.log.1", "api.log.2"]
    assert all(0 < path.stat().st_size <= 512 for path in files)
    content = "\n".join(path.read_text(encoding="utf-8") for path in files)
    timestamps = {
        datetime.fromisoformat(json.loads(line)["timestamp"]).timestamp()
        for line in content.splitlines()
        if line
    }
    assert {38, 39} <= timestamps
    assert 0 not in timestamps
    assert "private" not in content


def test_rotating_one_service_preserves_other_service_logs(tmp_path, loggers):
    consumer = loggers("consumer")
    consumer.info("consumer record")
    local = loggers(None)
    local.info("local record")
    api = loggers("api")
    for index in range(20):
        api.info("api %s %s", index, "x" * 180)
    assert json.loads((tmp_path / "consumer.log").read_text())["service"] == "consumer"
    assert "local record" in (tmp_path / "app.log").read_text()
    assert not (tmp_path / "consumer.log.1").exists()
    assert not (tmp_path / "app.log.1").exists()
