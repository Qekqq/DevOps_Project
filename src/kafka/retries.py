"""Сохранённые в PostgreSQL повторы фоновых прогнозов, отдельно от очереди Kafka."""

import logging
from datetime import datetime, timedelta, timezone
from threading import Event, Thread

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from src.db.database import get_session_factory
from src.db.models import ModelVersion, ShadowRetry, Study
from src.db.repositories import get_study
from src.kafka.shadow import predict_challengers
from src.telemetry import event


def defer_predictions(message, versions):
    """Подтверждать Kafka можно только после commit всех задач повтора."""
    with get_session_factory()() as db:
        from datetime import date

        study = get_study(
            db, message["patient_code"], date.fromisoformat(message["study_date"])
        )
        if study is None:
            raise ValueError("Исследование для фонового повтора не найдено")
        models = db.scalars(
            select(ModelVersion).where(ModelVersion.model_version.in_(versions))
        ).all()
        if {model.model_version for model in models} != set(versions):
            raise ValueError("Версия модели для фонового повтора не найдена")
        for model in models:
            db.execute(
                insert(ShadowRetry)
                .values(
                    study_id=study.id,
                    model_version_id=model.id,
                    next_attempt_at=datetime.now(timezone.utc) + timedelta(seconds=30),
                )
                .on_conflict_do_nothing()
            )
        db.commit()
    event("shadow_prediction_deferred", level=logging.WARNING)


def retry_task(study_id, model_id):
    with get_session_factory()() as db:
        task = db.scalar(
            select(ShadowRetry)
            .where(
                ShadowRetry.study_id == study_id,
                ShadowRetry.model_version_id == model_id,
                ShadowRetry.next_attempt_at <= datetime.now(timezone.utc),
            )
            .with_for_update(skip_locked=True)
        )
        if task is None:
            return
        model = db.get(ModelVersion, model_id)
        study = db.get(Study, study_id)
        if model.role != "challenger":
            # Активировать архивные модели ради старой задачи нельзя.
            db.delete(task)
            db.commit()
            return
        message = {
            "patient_code": study.patient_code,
            "study_date": study.study_date.isoformat(),
            "features": study.features,
            "model_version": "",  # Рассчитываем только указанную фоновую версию.
        }
        try:
            # Используем текущие показатели; существующий прогноз пропускается.
            # Commit прогноза предшествует удалению задачи. Повтор после сбоя безопасен.
            predict_challengers(message, only_versions={model.model_version})
        except Exception as error:
            task.attempts += 1
            delay = min(1800, 30 * 2 ** min(task.attempts, 6))
            task.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
            event(
                "shadow_retry_failed",
                level=logging.WARNING,
                error_type=type(error).__name__,
            )
        else:
            db.delete(task)
            event("shadow_retry_completed")
        db.commit()


def retry_pending():
    with get_session_factory()() as db:
        keys = db.execute(
            select(ShadowRetry.study_id, ShadowRetry.model_version_id)
            .where(ShadowRetry.next_attempt_at <= datetime.now(timezone.utc))
            .order_by(
                ShadowRetry.next_attempt_at,
                ShadowRetry.study_id,
                ShadowRetry.model_version_id,
            )
            .limit(20)
        ).all()
    for study_id, model_id in keys:
        try:
            retry_task(study_id, model_id)
        except Exception as error:
            # Сбой одной задачи не мешает попробовать остальные.
            event(
                "shadow_retry_failed",
                level=logging.ERROR,
                error_type=type(error).__name__,
            )


def start_retry_worker():
    stop = Event()

    def work():
        while not stop.is_set():
            try:
                retry_pending()
            except Exception as error:
                event(
                    "shadow_retry_failed",
                    level=logging.ERROR,
                    error_type=type(error).__name__,
                )
            stop.wait(5)

    thread = Thread(target=work, name="shadow-retries", daemon=True)
    thread.start()
    return stop
