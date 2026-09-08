from datetime import datetime, timedelta, timezone

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src import auth
from src.db.database import get_db
from src.db.models import User, UserSession
from src.passwords import hash_password

PASSWORD = "Пароль тестового пользователя"


@pytest.fixture
def setup():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def sqlite_now(connection, _):
        connection.create_function(
            "now", 0, lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        )

    User.__table__.create(engine)
    UserSession.__table__.create(engine)
    with Session(engine) as db:
        db.add(
            User(
                id=1,
                username="test",
                password_hash=hash_password(PASSWORD),
                role="user",
                is_active=True,
            )
        )
        db.commit()
    application = FastAPI()
    application.include_router(auth.router)

    @application.post("/predict", dependencies=[Depends(auth.current_user)])
    def predict():
        return {"prediction": 0}

    def database():
        with Session(engine) as db:
            yield db

    application.dependency_overrides[get_db] = database
    auth._attempts.clear()
    with TestClient(
        application, headers={"X-Requested-With": "DiabetesPredict"}
    ) as client:
        yield client, engine
    engine.dispose()


def login(client):
    response = client.post(
        "/auth/login", json={"username": "test", "password": PASSWORD}
    )
    assert response.status_code == 200
    assert "password_hash" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "access_token" not in response.json()
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    return {"X-CSRF-Token": response.json()["csrf_token"]}


def test_role_is_read_from_database_and_logout_revokes_session(setup):
    client, engine = setup
    assert client.post("/predict").status_code == 401
    headers = login(client)
    assert client.post("/predict", headers=headers).status_code == 200
    assert client.get("/auth/admin-access", headers=headers).status_code == 403
    with Session(engine) as db:
        db.get(User, 1).role = "admin"
        db.commit()
    assert client.get("/auth/admin-access", headers=headers).status_code == 200
    assert client.get("/auth/me", headers=headers).json()["user"]["role"] == "admin"
    assert client.post("/auth/logout", headers=headers).status_code == 204
    assert client.get("/auth/me", headers=headers).status_code == 401


@pytest.mark.parametrize("change", ["disabled", "password", "expired"])
def test_session_revocation(setup, change):
    client, engine = setup
    headers = login(client)
    with Session(engine) as db:
        user = db.get(User, 1)
        if change == "disabled":
            user.is_active = False
        elif change == "password":
            user.password_hash = hash_password("Другой пароль пользователя")
        else:
            db.scalar(select(UserSession)).expires_at = datetime.now(
                timezone.utc
            ) - timedelta(seconds=1)
        db.commit()
    assert client.get("/auth/me", headers=headers).status_code == 401


def test_bad_password_and_unknown_user_have_same_response(setup):
    client, _ = setup
    first = client.post("/auth/login", json={"username": "test", "password": "wrong"})
    second = client.post(
        "/auth/login", json={"username": "unknown", "password": "wrong"}
    )
    assert first.status_code == second.status_code == 401
    assert first.json() == second.json()


def test_disabled_user_cannot_login_and_token_is_not_stored(setup):
    client, engine = setup
    login(client)
    with Session(engine) as db:
        record = db.scalar(select(UserSession))
        token = client.cookies.get(auth.COOKIE_NAME)
        assert record.token_hash != token
        assert record.token_hash == auth.fingerprint(token)
        db.get(User, 1).is_active = False
        db.commit()
    response = client.post(
        "/auth/login", json={"username": "test", "password": PASSWORD}
    )
    assert response.status_code == 401


def test_login_rejects_client_role_and_throttles(setup):
    client, _ = setup
    assert (
        client.post(
            "/auth/login",
            json={"username": "test", "password": PASSWORD, "role": "admin"},
        ).status_code
        == 422
    )
    auth._attempts.extend([auth.time.monotonic()] * 10)
    assert (
        client.post(
            "/auth/login", json={"username": "test", "password": PASSWORD}
        ).status_code
        == 429
    )


def test_cookie_requires_csrf_for_writes_and_survives_reload(setup):
    client, _ = setup
    headers = login(client)
    assert client.post("/predict").status_code == 403
    assert client.post("/predict", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.post("/predict", headers=headers).status_code == 200
    assert client.post("/auth/logout").status_code == 403
    restored = client.get("/auth/me").json()
    assert restored["csrf_token"] == headers["X-CSRF-Token"]
    assert 0 < restored["expires_in"] <= auth.SESSION_SECONDS
    assert client.post("/auth/logout", headers=headers).status_code == 204
    assert client.cookies.get(auth.COOKIE_NAME) is None


def test_cross_origin_login_and_authenticated_write_rejected(setup):
    client, _ = setup
    payload = {"username": "test", "password": PASSWORD}
    assert (
        client.post(
            "/auth/login", json=payload, headers={"Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/auth/login", json=payload, headers={"X-Requested-With": ""}
        ).status_code
        == 403
    )
    headers = login(client)
    assert (
        client.post(
            "/predict", headers={**headers, "Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/predict", headers={**headers, "Origin": "http://testserver"}
        ).status_code
        == 200
    )


def test_login_rotates_previous_cookie_and_csrf(setup):
    client, _ = setup
    first = login(client)
    old_token = client.cookies.get(auth.COOKIE_NAME)
    second = login(client)
    assert first != second
    assert client.cookies.get(auth.COOKIE_NAME) != old_token
    assert client.post("/predict", headers=first).status_code == 403
    client.cookies.set(auth.COOKIE_NAME, old_token)
    assert client.get("/auth/me").status_code == 401


def test_secure_cookie_setting_for_https(setup, monkeypatch):
    client, _ = setup
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "true")
    response = client.post(
        "/auth/login", json={"username": "test", "password": PASSWORD}
    )
    assert response.status_code == 200
    assert "Secure" in response.headers["set-cookie"]
