"""Проверка API и Kafka на отдельном тестовом исследовании в работающем стенде."""

import json
import secrets
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from http.cookies import SimpleCookie
from uuid import uuid4

from sqlalchemy import select

from src.db.database import get_session_factory
from src.db.models import ModelVersion, PredictionHistory, Study, User
from src.passwords import hash_password

TOKEN = None
CSRF = None


def request(payload):
    query = urllib.request.Request(
        "http://frontend/api/predict",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Cookie": f"dp_session={TOKEN}",
            "X-CSRF-Token": CSRF,
        },
        method="POST",
    )
    with urllib.request.urlopen(query, timeout=20) as response:
        return json.load(response)


def check_prediction_flow():
    payload = dict(
        patient_code="ITG001",
        study_date="2026-09-08",
        pregnancies=6,
        glucose=148,
        blood_pressure=72,
        skin_thickness=35,
        insulin=0,
        bmi=33.6,
        diabetes_pedigree_function=0.627,
        age=50,
    )
    factory = get_session_factory()
    with factory() as db:
        existing = db.scalar(
            select(Study.id).where(
                Study.patient_code == payload["patient_code"],
                Study.study_date == date.fromisoformat(payload["study_date"]),
            )
        )
        if existing is not None:
            raise RuntimeError(
                "Тестовое исследование уже существует; повторный запуск не проверит доставку Kafka"
            )
        expected = set(
            db.scalars(
                select(ModelVersion.model_version).where(
                    ModelVersion.role.in_(["champion", "challenger"]),
                )
            )
        )
        assert len(expected) == 2, "Ожидаются две активные модели"
    first = request(payload)
    deadline = time.monotonic() + 50
    while True:
        with factory() as db:
            records = db.scalars(
                select(PredictionHistory)
                .join(Study)
                .where(
                    Study.patient_code == payload["patient_code"],
                    Study.study_date == date.fromisoformat(payload["study_date"]),
                )
            ).all()
            if {row.model_version.model_version for row in records} == expected:
                assert len(records) == 2
                break
        if time.monotonic() >= deadline:
            raise RuntimeError("Kafka consumer не сохранил прогнозы обеих моделей")
        time.sleep(1)
    repeated = request(payload)
    assert repeated["cached"] is True
    assert {key: value for key, value in repeated.items() if key != "cached"} == {
        key: value for key, value in first.items() if key != "cached"
    }, "Повторный запрос должен вернуть сохранённый результат"
    try:
        request({**payload, "glucose": 149})
    except urllib.error.HTTPError as error:
        assert error.code == 409
    else:
        raise AssertionError(
            "Изменение показателей существующего исследования должно вернуть 409"
        )

    def concurrent_request(glucose):
        try:
            request({**payload, "patient_code": "ITG002", "glucose": glucose})
            return 200
        except urllib.error.HTTPError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(concurrent_request, [130, 140]))
    assert sorted(statuses) == [200, 409], statuses
    print("Проверено: API, Kafka, два прогноза, повтор и параллельный конфликт.")
    print("Тестовое исследование ITG001 от 2026-09-08 сохранено без обратной связи.")


def test_authenticated_prediction_flow():
    global TOKEN, CSRF
    username = "ci_" + uuid4().hex
    password = secrets.token_urlsafe(32)
    factory = get_session_factory()
    with factory() as db:
        user = User(
            username=username, password_hash=hash_password(password), role="user"
        )
        db.add(user)
        db.commit()
        user_id = user.id
    try:
        query = urllib.request.Request(
            "http://frontend/api/auth/login",
            data=json.dumps({"username": username, "password": password}).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Requested-With": "DiabetesPredict",
            },
            method="POST",
        )
        with urllib.request.urlopen(query, timeout=20) as response:
            cookie = SimpleCookie(response.headers["Set-Cookie"])
            assert cookie["dp_session"]["httponly"]
            TOKEN = cookie["dp_session"].value
            CSRF = json.load(response)["csrf_token"]
        with urllib.request.urlopen("http://frontend/", timeout=20) as response:
            assert "Diabetes Predict" in response.read().decode()
        check_prediction_flow()
    finally:
        with factory() as db:
            db.get(User, user_id).is_active = False
            db.commit()
        TOKEN = None
        CSRF = None
