from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import MagicMock

from sqlalchemy.engine import make_url

from src.db import database


def test_database_password_round_trips_reserved_characters():
    password = "a b+c/@:#%?"
    url = make_url(
        database.build_database_url(
            {
                "POSTGRES_USER": "test user",
                "POSTGRES_PASSWORD": password,
                "POSTGRES_HOST": "db",
                "POSTGRES_PORT": "5432",
                "POSTGRES_DB": "diabetes",
            }
        )
    )
    assert url.password == password
    assert url.username == "test user"


def test_concurrent_first_requests_share_one_pool(monkeypatch):
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_session_factory", None)
    monkeypatch.setattr(database, "get_database_secrets", lambda: {})
    monkeypatch.setattr(database, "build_database_url", lambda _: "test")
    entered, release = Event(), Event()
    engine = MagicMock()

    def create(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return engine

    create_mock = MagicMock(side_effect=create)
    monkeypatch.setattr(database, "create_engine", create_mock)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(database.get_session_factory) for _ in range(4)]
        assert entered.wait(5)
        release.set()
        factories = [future.result(timeout=5) for future in futures]
    assert all(factory is factories[0] for factory in factories)
    assert create_mock.call_count == 1
