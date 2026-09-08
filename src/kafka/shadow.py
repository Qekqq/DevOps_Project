"""Фоновые прогнозы challenger для того же исследования."""

import time
from datetime import date

from sqlalchemy import select

from src.db.database import get_session_factory
from src.db.models import ModelVersion
from src.db.repositories import (
    DuplicatePredictionError,
    get_prediction_for_study,
    save_prediction_history,
)
from src.logger import get_logger
from src.model_registry import ModelRegistry

registry = ModelRegistry()
logger = get_logger(__name__)


def predict_challengers(message):
    session_factory = get_session_factory()
    study_date = date.fromisoformat(message["study_date"])
    with session_factory() as db:
        versions = list(
            db.execute(
                select(ModelVersion.model_version).where(
                    ModelVersion.role == "challenger",
                    ModelVersion.model_version != message["model_version"],
                )
            )
            .scalars()
            .all()
        )
    failures = []
    for version in versions:
        try:
            with session_factory() as db:
                # Повторно проверяем роль: отключённые версии больше не запускаем.
                model = db.execute(
                    select(ModelVersion).where(
                        ModelVersion.model_version == version,
                        ModelVersion.role == "challenger",
                    )
                ).scalar_one_or_none()
                if model is None:
                    continue
                saved = get_prediction_for_study(
                    db,
                    patient_code=message["patient_code"],
                    study_date=study_date,
                    model_version=version,
                    features=message["features"],
                )
                if saved is not None:
                    continue
                started = time.perf_counter()
                result = registry.from_record(model).predict(message["features"])
                save_prediction_history(
                    db,
                    features=message["features"],
                    prediction=result["prediction"],
                    probability=result.get("probability"),
                    model_version=model,
                    study_date=study_date,
                    patient_code=message["patient_code"],
                    request_source=message.get("request_source", "api"),
                    response_time_ms=int((time.perf_counter() - started) * 1000),
                    role_at_prediction="challenger",
                )
                db.commit()
        except DuplicatePredictionError:
            pass
        except Exception:
            logger.exception("Ошибка фонового прогноза версии %s", version)
            failures.append(version)
    if failures:
        # Успешные результаты уже зафиксированы. При повторе они пропускаются.
        raise RuntimeError("Не завершены фоновые прогнозы: " + ", ".join(failures))
