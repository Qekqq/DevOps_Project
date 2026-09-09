"""Агрегаты качества входов, прогнозов и смещения относительно train модели.

В Python поступают только счётчики и границы интервалов, без данных пациентов.
"""

from datetime import datetime, timedelta, timezone

import numpy as np
from sqlalchemy import Float, and_, cast, func, select

from src.db.models import (
    ModelVersion,
    PredictionFeedback,
    PredictionHistory,
    RawDatasetSample,
    Study,
    TrainingRun,
)
from src.features import FEATURE_COLUMNS, ZERO_AS_MISSING_COLUMNS

MIN_DRIFT_SAMPLES = 100
PSI_THRESHOLD = 0.25
TARGET_THRESHOLD = 0.10


def month_window(today=None):
    """Последние 30 дат исследования, включая сегодня (UTC), без будущих дат."""
    today = today or datetime.now(timezone.utc).date()
    return today - timedelta(days=29), today + timedelta(days=1)


def population_stability_index(reference, current):
    """PSI по одним интервалам; сглаживание предотвращает деление на ноль."""
    reference, current = np.asarray(reference, float), np.asarray(current, float)
    if min(reference.sum(), current.sum()) < MIN_DRIFT_SAMPLES:
        return float("nan")
    # Сглаживание долей не зависит от различия размеров выборок.
    p = np.maximum(reference / reference.sum(), 1e-4)
    q = np.maximum(current / current.sum(), 1e-4)
    p, q = p / p.sum(), q / q.sum()
    return float(np.sum((q - p) * np.log(q / p)))


def target_shift(reference_positive, reference_count, positive, count):
    """Изменение доли фактического класса 1; не смещение предсказаний."""
    if min(reference_count, count) < MIN_DRIFT_SAMPLES:
        return float("nan")
    return abs(positive / count - reference_positive / reference_count)


def classification_metrics(tp, tn, fp, fn):
    """Согласованные бинарные метрики; нулевой знаменатель означает NaN."""

    def ratio(numerator, denominator):
        return numerator / denominator if denominator else float("nan")

    return {
        "accuracy": ratio(tp + tn, tp + tn + fp + fn),
        "recall": ratio(tp, tp + fn),
        "specificity": ratio(tn, tn + fp),
        "precision": ratio(tp, tp + fp),
        "npv": ratio(tn, tn + fn),
        "f1": ratio(2 * tp, 2 * tp + fp + fn),
    }


def histogram_conditions(column, edges, zero_missing):
    valid = column.is_not(None)
    if zero_missing:
        valid = and_(valid, column != 0)
    edges = sorted(set(float(value) for value in (edges or [])))
    conditions = [column.is_(None)]
    if zero_missing:
        conditions.append(column == 0)
    if not edges:
        return conditions + [valid]
    # Отдельные хвосты и точка максимума обнаруживают выход за эталон,
    # в том числе когда эталонный признак константный.
    conditions.append(and_(valid, column < edges[0]))
    conditions.extend(
        and_(valid, column >= lower, column < upper)
        for lower, upper in zip(edges, edges[1:])
    )
    conditions.extend(
        [and_(valid, column == edges[-1]), and_(valid, column > edges[-1])]
    )
    return conditions


def feature_counts(db, source, columns, where, edges=None):
    expressions = [func.count().label("samples")]
    for name, column in columns.items():
        expressions.extend(
            [
                func.count().filter(column == 0).label(name + "_zeros"),
                func.count().filter(column.is_(None)).label(name + "_missing"),
            ]
        )
        if edges is not None:
            expressions.extend(
                func.count().filter(condition).label(f"{name}_bin_{index}")
                for index, condition in enumerate(
                    histogram_conditions(
                        column, edges[name], name in ZERO_AS_MISSING_COLUMNS
                    )
                )
            )
    return dict(
        db.execute(select(*expressions).select_from(source).where(where))
        .mappings()
        .one()
    )


def training_row_ids(configuration):
    """Не подменяем неизвестную обучающую выборку всем датасетом."""
    rows = configuration.get("dataset", {}).get("row_ids", {}).get("train")
    if (
        not isinstance(rows, list)
        or not rows
        or any(type(value) is not int or value < 0 for value in rows)
        or len(set(rows)) != len(rows)
    ):
        raise ValueError("Для версии модели не определены строки обучающей выборки")
    return rows


def read_model_reference(db, model_id=None):
    """Неизменный train-эталон; в динамике загружается один раз на запрос."""
    baseline = db.execute(
        select(
            ModelVersion.id,
            ModelVersion.model_version,
            TrainingRun.dataset_id,
            TrainingRun.configuration,
            ModelVersion.role,
        )
        .join(TrainingRun, TrainingRun.id == ModelVersion.training_run_id)
        .where(
            ModelVersion.id == model_id
            if model_id is not None
            else ModelVersion.role == "champion"
        )
    ).one_or_none()
    if baseline is None:
        return None
    model_id, version, dataset_id, configuration, role = baseline
    train_rows = training_row_ids(configuration)
    reference_where = and_(
        RawDatasetSample.dataset_id == dataset_id,
        RawDatasetSample.row_number.in_(train_rows),
    )
    ref_columns = {
        name: cast(RawDatasetSample.features[name].astext, Float)
        for name in FEATURE_COLUMNS
    }
    quantiles = []
    for name, column in ref_columns.items():
        valid = column.is_not(None)
        if name in ZERO_AS_MISSING_COLUMNS:
            valid = and_(valid, column != 0)
        quantiles.append(
            func.percentile_disc([i / 10 for i in range(11)])
            .within_group(column)
            .filter(valid)
            .label(name)
        )
    edges = dict(db.execute(select(*quantiles).where(reference_where)).mappings().one())
    reference = feature_counts(
        db, RawDatasetSample, ref_columns, reference_where, edges
    )
    if reference["samples"] != len(train_rows):
        raise ValueError("Часть строк обучающего эталона отсутствует в БД")
    ref_positive = db.scalar(
        select(func.count())
        .select_from(RawDatasetSample)
        .where(reference_where, RawDatasetSample.outcome == 1)
    )
    return {
        "model_id": model_id,
        "version": version,
        "dataset_id": dataset_id,
        "role": role,
        "edges": edges,
        "counts": reference,
        "positive": ref_positive,
    }


def read_model_health(
    db, today=None, model_id=None, *, start=None, end=None, reference_cache=None
):
    """Полуоткрытый интервал [start, end); по умолчанию 30 дат для Prometheus."""
    if start is None and end is None:
        start, end = month_window(today)
    elif start is None or end is None or start >= end:
        raise ValueError("Нужны обе границы периода; начало должно быть раньше конца")
    window = and_(Study.study_date >= start, Study.study_date < end)
    columns = {name: getattr(Study, name) for name in FEATURE_COLUMNS}
    quality = feature_counts(db, Study, columns, window)
    result = {"start": start, "end": end, "quality": quality, "reference": None}
    # Кэш принадлежит одному запросу/снимку БД, не переживает обновление моделей.
    cache = reference_cache if reference_cache is not None else {}
    if model_id not in cache:
        cache[model_id] = read_model_reference(db, model_id)
    baseline = cache[model_id]
    if baseline is None:
        return result
    model_id = baseline["model_id"]
    version, dataset_id, role = (
        baseline["version"],
        baseline["dataset_id"],
        baseline["role"],
    )
    edges, reference = baseline["edges"], baseline["counts"]
    ref_count, ref_positive = reference["samples"], baseline["positive"]
    # Если выпуск обучали на обратной связи, не сравниваем эти же исследования
    # с самими собой. Качество входных данных выше считается по всему окну.
    known_ids = select(RawDatasetSample.source_study_id).where(
        RawDatasetSample.dataset_id == dataset_id,
        RawDatasetSample.source_study_id.is_not(None),
    )
    fresh = and_(window, Study.id.not_in(known_ids))
    current = feature_counts(db, Study, columns, fresh, edges)
    drift = {}
    for name in FEATURE_COLUMNS:
        keys = [key for key in reference if key.startswith(name + "_bin_")]
        drift[name] = population_stability_index(
            [reference[key] for key in keys], [current[key] for key in keys]
        )
    labeled, positive = db.execute(
        select(func.count(), func.count().filter(PredictionFeedback.true_label == 1))
        .select_from(Study)
        .join(PredictionFeedback, PredictionFeedback.study_id == Study.id)
        .where(fresh)
    ).one()
    # Каждая версия оценивается по собственным прогнозам с фактическим исходом.
    # Исключаем снимок этой версии, а не обучающие данные других моделей.
    counts = dict(
        db.execute(
            select(
                *[
                    func.count()
                    .filter(
                        and_(
                            PredictionHistory.prediction == predicted,
                            PredictionFeedback.true_label == actual,
                        )
                    )
                    .label(name)
                    for name, actual, predicted in [
                        ("tp", 1, 1),
                        ("tn", 0, 0),
                        ("fp", 0, 1),
                        ("fn", 1, 0),
                    ]
                ],
            )
            .select_from(Study)
            .join(PredictionFeedback, PredictionFeedback.study_id == Study.id)
            .join(PredictionHistory, PredictionHistory.study_id == Study.id)
            .where(
                fresh,
                PredictionHistory.model_version_id == model_id,
            )
        )
        .mappings()
        .one()
    )
    metrics = classification_metrics(**counts)
    result.update(
        reference={
            "version": version,
            "role": role,
            "dataset_id": dataset_id,
            "samples": ref_count,
        },
        current_samples=current["samples"],
        drift=drift,
        labeled=labeled,
        positive_rate=positive / labeled if labeled else float("nan"),
        reference_positive_rate=ref_positive / ref_count if ref_count else float("nan"),
        target_shift=target_shift(ref_positive, ref_count, positive, labeled),
        evaluated=sum(counts.values()),
        accuracy=metrics["accuracy"],
        classification=metrics,
        confusion=counts,
    )
    return result


def read_model_health_snapshots(db):
    today = datetime.now(timezone.utc).date()
    ids = db.scalars(
        select(ModelVersion.id)
        .where(ModelVersion.role.in_(["champion", "challenger"]))
        .order_by((ModelVersion.role == "champion").desc(), ModelVersion.id)
    ).all()
    return [read_model_health(db, today, model_id) for model_id in ids] or [
        read_model_health(db, today)
    ]
