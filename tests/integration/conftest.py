"""Интеграционные тесты запускаются CD только на одноразовом стенде."""

import os

import pytest


def pytest_collection_modifyitems(items):
    for item in items:
        if "integration" not in item.path.parts:
            continue
        item.add_marker(pytest.mark.integration)
        if os.environ.get("RUN_INTEGRATION_TESTS") != "1":
            item.add_marker(pytest.mark.skip(reason="Нужен одноразовый стенд CD"))
