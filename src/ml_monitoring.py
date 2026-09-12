"""On-demand monitoring of current, version-specific predictions.

Only aggregates leave this module. Corrected inputs, predictions and labels are
read as known now; edits do not move the cohort. No reports are persisted.
"""

import os
import time
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
from sqlalchemy import func, select

from src.db.models import (
    ModelVersion,
    PredictionFeedback,
    PredictionHistory,
    RawDatasetSample,
    Study,
    TrainingRun,
)
from src.features import FEATURE_COLUMNS, ZERO_AS_MISSING_COLUMNS
from src.model_health import classification_metrics, training_row_ids

os.environ["DO_NOT_TRACK"] = "1"
MAX_POINTS = 400
MAX_SAMPLES = 50_000
MIN_DRIFT_SAMPLES = 100
PSI_THRESHOLD = 0.25


class ReportInputError(ValueError):
    """A safe, user-facing filter/resource validation error."""


def buckets(start, last, resolution):
    """Inclusive date range, disjoint calendar buckets, clipped edge buckets."""
    if resolution not in {"day", "week", "month"}:
        raise ReportInputError("Выберите день, неделю или месяц")
    if start > last or last == date.max:
        raise ReportInputError("Проверьте начало и конец периода")
    end = last + timedelta(days=1)
    result = []
    while start < end:
        limit = 1200 if resolution == "month" else MAX_POINTS
        if len(result) == limit:
            raise ReportInputError(
                f"Больше {limit} точек: увеличьте интервал или сократите период"
            )
        if resolution == "day":
            following = start + timedelta(days=1)
        elif resolution == "week":
            following = start + timedelta(days=7 - start.weekday())
        elif start.month == 12:
            following = date(start.year + 1, 1, 1) if start.year < 9999 else end
        else:
            following = date(start.year, start.month + 1, 1)
        following = min(following, end)
        result.append((start, following))
        start = following
    return result


def axis_column(axis):
    if axis == "prediction_time":
        return PredictionHistory.created_at
    if axis == "study_date":
        return Study.study_date
    raise ValueError("Неизвестная временная колонка")


def model_options(db):
    return [
        {"version": m.model_version, "name": m.model_name, "role": m.role}
        for m in db.scalars(
            select(ModelVersion).order_by(
                (ModelVersion.role == "champion").desc(), ModelVersion.id.desc()
            )
        )
    ]


def load_reference(db, model):
    run = db.get(TrainingRun, model.training_run_id)
    if run is None:
        return None, set(), "Для версии не найден обучающий эталон"
    rows = db.execute(
        select(
            RawDatasetSample.row_number,
            RawDatasetSample.features,
            RawDatasetSample.source_study_id,
        )
        .where(RawDatasetSample.dataset_id == run.dataset_id)
        .limit(MAX_SAMPLES + 1)
    ).all()
    if len(rows) > MAX_SAMPLES:
        raise ReportInputError("Обучающий эталон превышает лимит расчёта")
    excluded = {r.source_study_id for r in rows if r.source_study_id is not None}
    try:
        train = set(training_row_ids(run.configuration))
    except ValueError:
        return None, excluded, "Не определены строки train; drift не рассчитан"
    samples = [r.features for r in rows if r.row_number in train]
    if len(samples) != len(train):
        return None, excluded, "Часть train отсутствует; drift не рассчитан"
    return pd.DataFrame(samples, columns=FEATURE_COLUMNS, dtype=float), excluded, None


def clean_features(frame):
    result = (
        frame[list(FEATURE_COLUMNS)].astype(float).replace([np.inf, -np.inf], np.nan)
    )
    for name in ZERO_AS_MISSING_COLUMNS:
        result[name] = result[name].replace(0, np.nan)
    return result


def finite(value):
    return float(value) if value is not None and np.isfinite(value) else None


def evaluate(frame, reference):
    """All model metrics use labeled rows; data checks use all eligible rows."""
    from evidently import BinaryClassification, DataDefinition, Dataset, Report
    from evidently.metrics import (
        Accuracy,
        F1Score,
        LogLoss,
        MissingValueCount,
        Precision,
        Recall,
        RocAuc,
        ValueDrift,
    )

    labeled = frame.dropna(subset=["target"])
    actual, predicted = labeled.target, labeled.prediction
    counts = {
        name: int(((actual == a) & (predicted == p)).sum())
        for name, a, p in [("tp", 1, 1), ("tn", 0, 0), ("fp", 0, 1), ("fn", 1, 0)]
    }
    metrics = {name: finite(v) for name, v in classification_metrics(**counts).items()}
    metrics.update(roc_auc=None, log_loss=None)
    # Explicit metric objects keep the series lightweight, without HTML widgets.
    if len(labeled):
        definition = DataDefinition(
            classification=[
                BinaryClassification(
                    target="target",
                    prediction_labels="prediction",
                    prediction_probas=None,
                    pos_label=1,
                )
            ]
        )
        data = labeled[["target", "prediction", "probability"]].copy()
        data[["target", "prediction"]] = data[["target", "prediction"]].astype(int)
        chosen = {
            "accuracy": Accuracy(),
            "precision": Precision(),
            "recall": Recall(),
            "f1": F1Score(),
        }
        report = Report(list(chosen.values())).run(
            Dataset.from_pandas(
                data[["target", "prediction"]], data_definition=definition
            )
        )
        for name, metric in chosen.items():
            # Preserve undefined denominators instead of library zero-division defaults.
            if metrics[name] is not None:
                metrics[name] = finite(report.metric_results[metric.metric_id].value)
        if actual.nunique() == 2:
            probabilistic = {"roc_auc": RocAuc(), "log_loss": LogLoss()}
            definition = DataDefinition(
                classification=[
                    BinaryClassification(
                        target="target", prediction_probas="probability", pos_label=1
                    )
                ]
            )
            result = Report(list(probabilistic.values())).run(
                Dataset.from_pandas(
                    data[["target", "probability"]], data_definition=definition
                )
            )
            for name, metric in probabilistic.items():
                metrics[name] = finite(result.metric_results[metric.metric_id].value)
        else:
            # Unlike AUC, log loss is defined for a single observed class.
            probabilities = np.clip(
                data.probability.to_numpy(),
                np.finfo(float).eps,
                1 - np.finfo(float).eps,
            )
            metrics["log_loss"] = float(
                -np.mean(
                    data.target * np.log(probabilities)
                    + (1 - data.target) * np.log1p(-probabilities)
                )
            )
    current = clean_features(frame)
    missing = {name: int(current[name].isna().sum()) for name in FEATURE_COLUMNS}
    if len(current):
        missing_metrics = {
            name: MissingValueCount(column=name) for name in FEATURE_COLUMNS
        }
        result = Report(list(missing_metrics.values())).run(
            Dataset.from_pandas(
                current,
                data_definition=DataDefinition(numerical_columns=list(FEATURE_COLUMNS)),
            )
        )
        missing = {
            name: int(result.metric_results[metric.metric_id].count.value)
            for name, metric in missing_metrics.items()
        }
    drift = {}
    ref = clean_features(reference) if reference is not None else None
    for name in FEATURE_COLUMNS:
        n = int(current[name].notna().sum())
        nr = int(ref[name].notna().sum()) if ref is not None else 0
        score = None
        if min(n, nr) >= MIN_DRIFT_SAMPLES:
            metric = ValueDrift(column=name, method="psi", threshold=PSI_THRESHOLD)
            definition = DataDefinition(numerical_columns=[name])
            report = Report([metric]).run(
                Dataset.from_pandas(
                    current[[name]].dropna(), data_definition=definition
                ),
                Dataset.from_pandas(ref[[name]].dropna(), data_definition=definition),
            )
            score = finite(report.metric_results[metric.metric_id].value)
        drift[name] = {
            "psi": score,
            "detected": score >= PSI_THRESHOLD if score is not None else None,
            "samples": n,
            "reference_samples": nr,
        }
    return {
        "samples": len(frame),
        "evaluated": len(labeled),
        "missing_outcomes": len(frame) - len(labeled),
        "actual_positive": counts["tp"] + counts["fn"],
        "predicted_positive_rate": finite(frame.prediction.mean()),
        "actual_positive_rate": finite(actual.mean()),
        "metrics": metrics,
        "confusion": counts,
        "missing": missing,
        "missing_rate": finite(current.isna().to_numpy().mean())
        if len(frame)
        else None,
        "drift": drift,
        "small_sample": 0 < len(labeled) < MIN_DRIFT_SAMPLES,
    }


def read_report(
    db,
    *,
    model_version=None,
    date_from=None,
    date_to=None,
    time_axis="prediction_time",
    resolution="month",
):
    started = time.monotonic()
    models = model_options(db)
    version = model_version or (models[0]["version"] if models else None)
    model = db.scalar(select(ModelVersion).where(ModelVersion.model_version == version))
    if model is None:
        raise LookupError("Версия модели не найдена")
    column = axis_column(time_axis)
    base = (
        select(PredictionHistory)
        .join(Study)
        .where(PredictionHistory.model_version_id == model.id)
    )
    first, last = db.execute(
        select(func.min(column), func.max(column))
        .select_from(PredictionHistory)
        .join(Study)
        .where(PredictionHistory.model_version_id == model.id)
    ).one()

    def as_date(value):
        return (
            value.astimezone(timezone.utc).date()
            if isinstance(value, datetime)
            else value
        )

    first, last = as_date(first), as_date(last)
    today = datetime.now(timezone.utc).date()
    start, end = date_from or first or today, date_to or last or today
    intervals = buckets(start, end, resolution)
    lower, upper = start, end + timedelta(days=1)
    if time_axis == "prediction_time":
        lower, upper = [
            datetime.combine(v, datetime.min.time(), timezone.utc)
            for v in (lower, upper)
        ]
    rows = db.execute(
        base.with_only_columns(PredictionHistory, Study, PredictionFeedback.true_label)
        .outerjoin(PredictionFeedback, PredictionFeedback.study_id == Study.id)
        .where(column >= lower, column < upper)
        .order_by(PredictionHistory.id)
        .limit(MAX_SAMPLES + 1)
    ).all()
    if len(rows) > MAX_SAMPLES:
        raise ReportInputError("Больше 50 000 прогнозов: сократите период")
    columns = [
        *FEATURE_COLUMNS,
        "study_id",
        "timestamp",
        "target",
        "prediction",
        "probability",
    ]
    frame = pd.DataFrame(
        [
            {
                **study.features,
                "study_id": study.id,
                "timestamp": as_date(pred.created_at)
                if time_axis == "prediction_time"
                else study.study_date,
                "target": target,
                "prediction": pred.prediction,
                "probability": pred.probability,
            }
            for pred, study, target in rows
        ],
        columns=columns,
    )
    reference, known, reference_error = load_reference(db, model)
    excluded = int(frame.study_id.isin(known).sum())
    frame = frame.loc[~frame.study_id.isin(known)].copy()

    def point(a, b):
        if time.monotonic() - started > 45:
            raise TimeoutError(
                "Расчёт превысил 45 секунд; увеличьте интервал или сократите период"
            )
        subset = frame.loc[(frame.timestamp >= a) & (frame.timestamp < b)]
        return {"start": a, "end_exclusive": b, **evaluate(subset, reference)}

    summary = point(start, end + timedelta(days=1))
    points = [point(a, b) for a, b in intervals] if len(intervals) > 1 else [summary]
    return {
        "available_models": models,
        "model_version": version,
        "time_axis": time_axis,
        "timezone": "UTC",
        "resolution": resolution,
        "date_from": start,
        "date_to": end,
        "available_from": first,
        "available_to": last,
        "excluded_training": excluded,
        "reference_samples": len(reference) if reference is not None else 0,
        "reference_error": reference_error,
        "minimum_drift_samples": MIN_DRIFT_SAMPLES,
        "psi_threshold": PSI_THRESHOLD,
        "engine": "Evidently 0.7.23",
        "summary": summary,
        "points": points,
    }
