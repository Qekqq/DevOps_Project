"""Проверка API и Kafka на отдельном тестовом исследовании в работающем стенде."""

import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from sqlalchemy import select

from src.db.database import get_session_factory
from src.db.models import ModelVersion, PredictionHistory, Study


def request(payload):
    query = urllib.request.Request(
        "http://diabetes-api:8000/predict",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(query, timeout=20) as response:
        return json.load(response)


def main():
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
    assert request(payload) == first, (
        "Повторный запрос должен вернуть сохранённый результат"
    )
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


if __name__ == "__main__":
    main()
