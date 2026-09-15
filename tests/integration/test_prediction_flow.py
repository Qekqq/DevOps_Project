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

import pytest
from sqlalchemy import select

from src.config import get_project_root
from src.db.database import get_session_factory
from src.db.models import ModelVersion, PredictionHistory, Study, User
from src.passwords import hash_password

TOKEN = None
CSRF = None


def test_durable_shadow_retry_survives_failure_and_saves_once(monkeypatch):
    from contextlib import contextmanager
    from datetime import datetime, timedelta, timezone
    from unittest.mock import Mock

    from src.db.models import ShadowRetry
    from src.kafka import retries
    from src.kafka.shadow import ShadowPredictionError

    factory = get_session_factory()
    with factory() as db:
        model = db.scalar(select(ModelVersion).where(ModelVersion.role == "challenger"))
        if model is None:
            pytest.skip("В выпуске нет фоновых моделей для повторного расчёта")
        model_id, version = model.id, model.model_version
        study = Study(
            patient_code="RTY001",
            study_date=date(2026, 9, 10),
            features={
                "pregnancies": 0,
                "glucose": 120,
                "blood_pressure": 70,
                "skin_thickness": 20,
                "insulin": 0,
                "bmi": 30,
                "diabetes_pedigree_function": 0.5,
                "age": 40,
            },
        )
        db.add(study)
        db.flush()
        study_id = study.id
        message = {
            "patient_code": study.patient_code,
            "study_date": study.study_date.isoformat(),
        }
        db.commit()
    retries.defer_predictions(message, [version])
    retries.defer_predictions(message, [version])
    # Новый сеанс видит одну сохранённую задачу после повторной доставки Kafka.
    with factory() as db:
        tasks = db.scalars(
            select(ShadowRetry).where(ShadowRetry.study_id == study_id)
        ).all()
        assert len(tasks) == 1
        assert tasks[0].attempts == 0

    def execute_due(*, fail):
        with factory() as db:
            task = db.scalar(
                select(ShadowRetry)
                .where(ShadowRetry.study_id == study_id)
                .with_for_update()
            )
            task.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.flush()

            @contextmanager
            def session():
                yield db

            # Изменение срока пока не закоммичено: рабочий поток не перехватит задачу.
            with monkeypatch.context() as patch:
                patch.setattr(retries, "get_session_factory", lambda: session)
                if fail:
                    patch.setattr(
                        retries,
                        "predict_challengers",
                        Mock(side_effect=ShadowPredictionError([version])),
                    )
                retries.retry_task(study_id, model_id)

    execute_due(fail=True)
    with factory() as db:
        task = db.get(ShadowRetry, (study_id, model_id))
        assert task.attempts == 1
        assert task.next_attempt_at > datetime.now(timezone.utc)
    execute_due(fail=False)
    with factory() as db:
        assert db.get(ShadowRetry, (study_id, model_id)) is None
        predictions = db.scalars(
            select(PredictionHistory).where(
                PredictionHistory.study_id == study_id,
                PredictionHistory.model_version_id == model_id,
            )
        ).all()
        assert len(predictions) == 1
        assert predictions[0].prediction in (0, 1)
    # Повтор после сбоя между сохранением прогноза и удалением задачи не создаёт дубль.
    retries.defer_predictions(message, [version])
    execute_due(fail=False)
    with factory() as db:
        assert db.get(ShadowRetry, (study_id, model_id)) is None
        assert (
            len(
                db.scalars(
                    select(PredictionHistory).where(
                        PredictionHistory.study_id == study_id,
                        PredictionHistory.model_version_id == model_id,
                    )
                ).all()
            )
            == 1
        )


def request(payload):
    query = urllib.request.Request(
        "http://frontend:8080/api/predict",
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
        manifest = json.loads(
            (get_project_root() / "models/current.json").read_text(encoding="utf-8")
        )
        assert expected == {record["version"] for record in manifest["models"]}
        assert expected, "В выпуске нет активных моделей"
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
                assert len(records) == len(expected)
                break
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "Kafka consumer не сохранил прогнозы всех моделей выпуска"
            )
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
            "http://frontend:8080/api/auth/login",
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
        with urllib.request.urlopen("http://frontend:8080/", timeout=20) as response:
            assert "Diabetes Predict" in response.read().decode()
        check_prediction_flow()
        check_demo_load()
    finally:
        with factory() as db:
            db.get(User, user_id).is_active = False
            db.commit()
        TOKEN = None
        CSRF = None


def check_demo_load():
    """Small synthetic demo workload; verify accepted jobs are durably delivered."""

    def submit(index):
        payload = dict(
            patient_code=f"LOD{index:03d}",
            study_date="2026-09-11",
            pregnancies=0,
            glucose=100 + index,
            blood_pressure=70,
            skin_thickness=20,
            insulin=0,
            bmi=25,
            diabetes_pedigree_function=0.5,
            age=40,
        )
        started = time.monotonic()
        try:
            request(payload)
            status = 200
        except urllib.error.HTTPError as error:
            status = error.code
        return index, status, time.monotonic() - started

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(submit, range(24)))
    assert all(status in (200, 429, 503) for _, status, _ in results)
    accepted = [f"LOD{index:03d}" for index, status, _ in results if status == 200]
    assert accepted
    with urllib.request.urlopen(
        "http://frontend:8080/api/db/health", timeout=5
    ) as response:
        assert response.status == 200
    deadline = time.monotonic() + 60
    while True:
        with get_session_factory()() as db:
            active = set(
                db.scalars(
                    select(ModelVersion.id).where(
                        ModelVersion.role.in_(["champion", "challenger"])
                    )
                )
            )
            records = db.execute(
                select(Study.patient_code, PredictionHistory.model_version_id)
                .join(PredictionHistory)
                .where(Study.patient_code.in_(accepted))
            ).all()
        if set(records) == {
            (patient, model) for patient in accepted for model in active
        }:
            break
        assert time.monotonic() < deadline, (
            "Accepted workload was not durably processed"
        )
        time.sleep(1)
    assert max(duration for _, _, duration in results) < 20
