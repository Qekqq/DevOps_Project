"""Динамика по датам исследования, независимо от истории сборов Prometheus."""

import math
import time
from datetime import date, timedelta

from sqlalchemy import select

from src.db.models import ModelVersion
from src.features import ZERO_AS_MISSING_COLUMNS
from src.model_health import MIN_DRIFT_SAMPLES, read_model_health

MAX_PERIOD_DAYS = 366


def period_buckets(date_from: date, date_to: date, group_by="day"):
    """Обе выбранные даты включены; неполные месяцы обрезаются по периоду."""
    if group_by not in {"day", "month"}:
        raise ValueError("Группировка должна быть по дням или по месяцам")
    if not 1 <= (date_to - date_from).days + 1 <= MAX_PERIOD_DAYS:
        raise ValueError("Выберите период от 1 до 366 дней")
    if date_to == date.max:
        raise ValueError("Дата окончания выходит за поддерживаемый диапазон")
    end = date_to + timedelta(days=1)
    start = date_from
    buckets = []
    while start < end:
        if group_by == "day":
            following = start + timedelta(days=1)
        elif start.year == 9999 and start.month == 12:
            following = end
        elif start.month == 12:
            following = date(start.year + 1, 1, 1)
        else:
            following = date(start.year, start.month + 1, 1)
        following = min(following, end)
        buckets.append((start, following))
        start = following
    return buckets


def finite_or_none(value):
    return float(value) if math.isfinite(value) else None


def present_snapshot(snapshot):
    """JSON содержит только агрегаты; пустота и неопределённость не равны нулю."""
    quality = snapshot["quality"]
    samples = quality["samples"]
    reference_samples = snapshot["reference"]["samples"]

    def drift_status(count):
        if not count:
            return "Нет данных"
        if min(count, reference_samples) < MIN_DRIFT_SAMPLES:
            return "Недостаточно данных"
        return "Рассчитано"

    missing = {
        feature: quality[feature + "_zeros"] if samples else None
        for feature in ZERO_AS_MISSING_COLUMNS
    }
    return {
        "start": snapshot["start"],
        "end_exclusive": snapshot["end"],
        "data_quality": {
            "samples": samples,
            "status": "Рассчитано" if samples else "Нет данных",
            "missing_measurements": missing,
            "missing_total": sum(missing.values()) if samples else None,
        },
        "data_drift": {
            "samples": snapshot["current_samples"],
            "status": drift_status(snapshot["current_samples"]),
            "psi": {k: finite_or_none(v) for k, v in snapshot["drift"].items()},
        },
        "target_drift": {
            "samples": snapshot["labeled"],
            "status": drift_status(snapshot["labeled"]),
            "shift": finite_or_none(snapshot["target_shift"]),
            "positive_rate": finite_or_none(snapshot["positive_rate"]),
            "reference_positive_rate": finite_or_none(
                snapshot["reference_positive_rate"]
            ),
        },
        "prediction_quality": {
            "samples": snapshot["evaluated"],
            "status": "Рассчитано" if snapshot["evaluated"] else "Нет данных",
            "metrics": {
                k: finite_or_none(v) for k, v in snapshot["classification"].items()
            },
            "confusion_matrix": {
                k: v if snapshot["evaluated"] else None
                for k, v in snapshot["confusion"].items()
            },
        },
    }


def read_model_health_history(
    db, *, date_from, date_to, group_by="day", model_version=None, include_points=True
):
    """Каждый интервал и итог считаются из БД, без усреднения готовых метрик."""
    buckets = period_buckets(date_from, date_to, group_by)
    available = db.scalars(
        select(ModelVersion)
        .where(ModelVersion.role.in_(["champion", "challenger"]))
        .order_by((ModelVersion.role == "champion").desc(), ModelVersion.id)
    ).all()
    models = [m for m in available if model_version in (None, m.model_version)]
    if model_version is not None and not models:
        raise LookupError("Выбранная активная версия модели не найдена")
    cache = {}
    deadline = time.monotonic() + 20

    def snapshot(model, start, end):
        if time.monotonic() > deadline:
            raise TimeoutError("Расчёт занял слишком много времени; сократите период")
        result = read_model_health(
            db, model_id=model.id, start=start, end=end, reference_cache=cache
        )
        if time.monotonic() > deadline:
            raise TimeoutError("Расчёт занял слишком много времени; сократите период")
        if result["reference"] is None:
            raise ValueError("Не найден обучающий эталон модели")
        return result

    result = {
        "date_from": date_from,
        "date_to": date_to,
        "group_by": group_by,
        "date_field": "study_date",
        "minimum_drift_samples": MIN_DRIFT_SAMPLES,
        "available_models": [
            {
                "version": m.model_version,
                "name": {
                    "logistic_regression": "Логистическая регрессия",
                    "decision_tree": "Дерево решений",
                }.get(m.family, m.model_name),
                "role": m.role,
            }
            for m in available
        ],
        "models": [],
    }
    for model in models:
        total = snapshot(model, date_from, date_to + timedelta(days=1))
        result["models"].append(
            {
                "reference": total["reference"],
                "summary": present_snapshot(total),
                "points": [
                    present_snapshot(snapshot(model, start, end))
                    for start, end in buckets
                ]
                if include_points
                else [],
            }
        )
    return result
