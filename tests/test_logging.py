"""Ротация обычного журнала нескольких модулей и разделение сервисов."""

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
        (first if index % 2 == 0 else second).info("Запись %03d %s", index, "x" * 120)
    files = sorted(tmp_path.iterdir())
    assert [path.name for path in files] == ["api.log", "api.log.1", "api.log.2"]
    assert all(0 < path.stat().st_size <= 512 for path in files)
    content = "\n".join(path.read_text(encoding="utf-8") for path in files)
    assert "Запись 038" in content
    assert "Запись 039" in content
    assert "Запись 000" not in content


def test_rotating_one_service_preserves_other_service_logs(tmp_path, loggers):
    consumer = loggers("consumer")
    consumer.info("consumer record")
    local = loggers(None)
    local.info("local record")
    api = loggers("api")
    for index in range(20):
        api.info("api %s %s", index, "x" * 180)
    assert "consumer record" in (tmp_path / "consumer.log").read_text()
    assert "local record" in (tmp_path / "app.log").read_text()
    assert not (tmp_path / "consumer.log.1").exists()
    assert not (tmp_path / "app.log.1").exists()
