"""Вход, отзыв сессий и проверка актуальной роли в БД."""

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from threading import BoundedSemaphore

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import APIKeyCookie
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from src.db.database import get_db
from src.db.models import User, UserSession
from src.passwords import hash_password, verify_password
from src.request_limits import LoginLimits

router = APIRouter(prefix="/auth", tags=["Авторизация"])
COOKIE_NAME = "dp_session"
SESSION_SECONDS = 8 * 3600
cookie = APIKeyCookie(name=COOKIE_NAME, auto_error=False)
_dummy_hash = hash_password(secrets.token_urlsafe(32))
_login_limits = LoginLimits()
_password_slots = BoundedSemaphore(2)


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=128, repr=False)


def public_user(user):
    return {"id": user.id, "username": user.username, "role": user.role}


def cookie_secure():
    return os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"


def check_origin(request: Request):
    origin = request.headers.get("origin")
    expected = (
        os.environ.get("PUBLIC_ORIGIN")
        or f"{request.url.scheme}://{request.url.netloc}"
    )
    if origin is not None and origin != expected:
        raise HTTPException(403, "Недопустимый источник запроса")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Недопустимый источник запроса")


def csrf_token(token: str) -> str:
    return hmac.new(
        token.encode(), b"diabetes-predict-csrf", hashlib.sha256
    ).hexdigest()


def check_csrf(request: Request, token: str):
    check_origin(request)
    supplied = request.headers.get("x-csrf-token", "")
    if not hmac.compare_digest(supplied.encode(), csrf_token(token).encode()):
        raise HTTPException(403, "Обновите страницу и повторите запрос")


def login_guard(request: Request):
    check_origin(request)
    # Не даёт сторонней HTML-форме выполнить вход от имени посетителя.
    # Cross-origin fetch с этим заголовком требует запрещённого CORS preflight.
    if request.headers.get("x-requested-with") != "DiabetesPredict":
        raise HTTPException(403, "Отсутствует заголовок X-Requested-With")


def required_credentials(token: str | None = Depends(cookie)):
    if token is None or len(token) != 43:
        raise HTTPException(401, "Войдите в систему")
    return token


def current_user(
    request: Request,
    credentials: str = Depends(required_credentials),
    db: Session = Depends(get_db),
):
    error = HTTPException(401, "Войдите в систему")
    session = db.get(UserSession, fingerprint(credentials))
    if session is None or session.expires_at.replace(
        tzinfo=timezone.utc
    ) <= datetime.now(timezone.utc):
        raise error
    user = db.get(User, session.user_id)
    if (
        user is None
        or not user.is_active
        or user.role not in {"user", "admin"}
        or session.password_fingerprint != fingerprint(user.password_hash)
    ):
        raise error
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        check_csrf(request, credentials)
    request.state.auth_session = session
    return user


def require_admin(user: User = Depends(current_user)):
    if user.role != "admin":
        raise HTTPException(403, "Доступ разрешён только администратору")
    return user


@router.post("/login", dependencies=[Depends(login_guard)], summary="Войти в систему")
def login(
    data: LoginInput,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    # No raw username is retained in the limiter. Nginx also limits by client IP.
    if not _login_limits.admit(fingerprint(data.username)):
        raise HTTPException(
            429,
            "Слишком много попыток. Повторите через минуту.",
            headers={"Retry-After": "60"},
        )
    if not _password_slots.acquire(blocking=False):
        raise HTTPException(
            429,
            "Вход занят. Повторите через несколько секунд.",
            headers={"Retry-After": "2"},
        )
    try:
        user = db.scalar(select(User).where(User.username == data.username))
        valid = verify_password(
            data.password, user.password_hash if user else _dummy_hash
        )
    finally:
        _password_slots.release()
    if (
        not valid
        or user is None
        or not user.is_active
        or user.role not in {"admin", "user"}
    ):
        raise HTTPException(401, "Неверное имя пользователя или пароль")
    token = secrets.token_urlsafe(32)
    now_utc = datetime.now(timezone.utc)
    db.execute(delete(UserSession).where(UserSession.expires_at <= now_utc))
    previous = request.cookies.get(COOKIE_NAME)
    if previous:
        db.execute(
            delete(UserSession).where(UserSession.token_hash == fingerprint(previous))
        )
    db.add(
        UserSession(
            token_hash=fingerprint(token),
            user_id=user.id,
            password_fingerprint=fingerprint(user.password_hash),
            expires_at=now_utc + timedelta(hours=8),
        )
    )
    db.commit()
    from src.telemetry import event

    event("login_succeeded", actor_id=user.id)
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_SECONDS,
        httponly=True,
        secure=cookie_secure(),
        samesite="strict",
        path="/",
    )
    return {
        "csrf_token": csrf_token(token),
        "expires_in": SESSION_SECONDS,
        "user": public_user(user),
    }


@router.get("/me", summary="Текущий пользователь и сессия")
def me(request: Request, response: Response, user: User = Depends(current_user)):
    response.headers["Cache-Control"] = "no-store"
    session = request.state.auth_session
    remaining = max(
        0,
        int(
            (
                session.expires_at.replace(tzinfo=timezone.utc)
                - datetime.now(timezone.utc)
            ).total_seconds()
        ),
    )
    return {
        "user": public_user(user),
        "csrf_token": csrf_token(request.cookies[COOKIE_NAME]),
        "expires_in": remaining,
    }


@router.post("/logout", status_code=204, summary="Выйти из системы")
def logout(
    request: Request,
    response: Response,
    credentials: str = Depends(required_credentials),
    db: Session = Depends(get_db),
):
    check_csrf(request, credentials)
    db.execute(
        delete(UserSession).where(UserSession.token_hash == fingerprint(credentials))
    )
    db.commit()
    response.delete_cookie(
        COOKIE_NAME, path="/", httponly=True, secure=cookie_secure(), samesite="strict"
    )
    response.headers["Cache-Control"] = "no-store"


@router.get(
    "/admin-access",
    dependencies=[Depends(require_admin)],
    summary="Права администратора",
)
def admin_access():
    return {"allowed": True}
