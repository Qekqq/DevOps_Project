"""Качество каждой активной модели по её прогнозам с фактическим исходом."""

from datetime import date, timedelta
from threading import BoundedSemaphore
from typing import Literal

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sklearn.metrics import classification_report
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from src.auth import require_admin
from src.db.database import get_session_factory
from src.db.models import (
    ModelVersion,
    PredictionFeedback,
    PredictionHistory,
    Study,
    User,
)
from src.ml_monitoring import ReportInputError, read_report
from src.model_health import month_window
from src.model_health_history import period_buckets, read_model_health_history

router = APIRouter(prefix="/monitoring", tags=["Мониторинг"])
history_slots = BoundedSemaphore(2)
report_slots = BoundedSemaphore(1)


@router.get(
    "/model-report", summary="Качество и drift по времени прогноза или исследования"
)
def model_report(
    response: Response,
    user: User = Depends(require_admin),
    model_version: str | None = Query(default=None, min_length=1, max_length=50),
    date_from: date | None = None,
    date_to: date | None = None,
    time_axis: Literal["prediction_time", "study_date"] = "prediction_time",
    resolution: Literal["day", "week", "month"] = "month",
):
    response.headers["Cache-Control"] = "no-store"
    if not report_slots.acquire(blocking=False):
        raise HTTPException(503, "Расчёт уже выполняется; повторите запрос позже")
    try:
        with get_session_factory()() as db:
            db.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            db.execute(text("SET LOCAL statement_timeout = '5s'"))
            return read_report(
                db,
                model_version=model_version,
                date_from=date_from,
                date_to=date_to,
                time_axis=time_axis,
                resolution=resolution,
            )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except ReportInputError as exc:
        raise HTTPException(422, str(exc)) from None
    except TimeoutError as exc:
        raise HTTPException(503, str(exc)) from None
    except (SQLAlchemyError, ValueError):
        raise HTTPException(503, "Не удалось рассчитать отчёт мониторинга") from None
    finally:
        report_slots.release()


def quality_report(tp, fn, fp, tn):
    """Отчёт sklearn по четырём комбинациям без разворачивания всей выборки."""
    if tp + fn + fp + tn == 0:
        return {
            name: {
                "precision": float("nan"),
                "recall": float("nan"),
                "f1-score": float("nan"),
                "support": 0,
            }
            for name in ("0", "1", "macro avg", "weighted avg")
        } | {"accuracy": float("nan")}
    return classification_report(
        [0, 0, 1, 1],
        [0, 1, 0, 1],
        sample_weight=[tn, fp, fn, tp],
        labels=[0, 1],
        output_dict=True,
        zero_division=np.nan,
    )


@router.get("/access", include_in_schema=False)
def monitoring_access(user: User = Depends(require_admin)):
    """Nginx проверяет текущую сессию перед каждым обращением к Grafana."""
    return Response(status_code=204, headers={"X-Monitoring-User": f"user-{user.id}"})


@router.get("/model-health", summary="Метрики моделей за период и их динамика")
def model_health_history(
    response: Response,
    user: User = Depends(require_admin),
    date_from: date | None = None,
    date_to: date | None = None,
    group_by: Literal["day", "month"] = "day",
    model_version: str | None = Query(default=None, min_length=1, max_length=50),
    include_points: bool = True,
):
    if date_from is None and date_to is None:
        date_from, end = month_window()
        date_to = end - timedelta(days=1)
    elif date_from is None or date_to is None:
        raise HTTPException(422, "Укажите начало и окончание периода")
    try:
        period_buckets(date_from, date_to, group_by)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    response.headers["Cache-Control"] = "no-store"
    if not history_slots.acquire(blocking=False):
        raise HTTPException(503, "Расчёт метрик занят; повторите запрос позже")
    try:
        # Отдельная сессия: авторизация уже читала БД в своей транзакции.
        with get_session_factory()() as db:
            db.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            db.execute(text("SET LOCAL statement_timeout = '5s'"))
            return read_model_health_history(
                db,
                date_from=date_from,
                date_to=date_to,
                group_by=group_by,
                model_version=model_version,
                include_points=include_points,
            )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except TimeoutError as exc:
        raise HTTPException(503, str(exc)) from None
    except (SQLAlchemyError, ValueError):
        raise HTTPException(
            503, "Не удалось рассчитать метрики; проверьте БД и обучающий эталон модели"
        ) from None
    finally:
        history_slots.release()


def read_quality_snapshot(db):
    models = db.scalars(
        select(ModelVersion)
        .where(ModelVersion.role.in_(["champion", "challenger"]))
        .order_by(ModelVersion.id)
    ).all()
    ids = [model.id for model in models]
    # Полнота доставки — отдельный показатель, не фильтр качества моделей.
    complete = (
        select(PredictionHistory.study_id)
        .where(PredictionHistory.model_version_id.in_(ids))
        .group_by(PredictionHistory.study_id)
        .having(func.count() == len(ids))
    )
    predicted = select(PredictionHistory.study_id).where(
        PredictionHistory.model_version_id.in_(ids)
    )
    cohort = select(PredictionFeedback.study_id).where(
        PredictionFeedback.study_id.in_(predicted)
    )
    grouped = db.execute(
        select(
            PredictionHistory.model_version_id,
            PredictionFeedback.true_label,
            PredictionHistory.prediction,
            func.count(),
        )
        .join(
            PredictionFeedback,
            PredictionFeedback.study_id == PredictionHistory.study_id,
        )
        .where(PredictionHistory.model_version_id.in_(ids))
        .group_by(
            PredictionHistory.model_version_id,
            PredictionFeedback.true_label,
            PredictionHistory.prediction,
        )
    ).all()
    counts = {model.id: {"tp": 0, "fn": 0, "fp": 0, "tn": 0} for model in models}
    names = {(1, 1): "tp", (1, 0): "fn", (0, 1): "fp", (0, 0): "tn"}
    for model_id, actual, predicted, count in grouped:
        counts[model_id][names[actual, predicted]] = count
    pending = select(Study.id).where(~Study.id.in_(complete))
    return {
        "studies": db.scalar(select(func.count()).select_from(Study)),
        "feedback": db.scalar(select(func.count()).select_from(PredictionFeedback)),
        "cohort": db.scalar(select(func.count()).select_from(cohort.subquery())),
        "pending": db.scalar(select(func.count()).select_from(pending.subquery()))
        if ids
        else 0,
        "models": [
            {
                "version": model.model_version,
                "name": {
                    "logistic_regression": "Logistic Regression",
                    "decision_tree": "Decision Tree",
                }.get(model.family, model.model_name),
                "role": model.role,
                "counts": counts[model.id],
                "report": quality_report(**counts[model.id]),
            }
            for model in models
        ],
    }
