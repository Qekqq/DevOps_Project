"""Отдельный сборщик: ошибка drift не скрывает существующие метрики качества."""

import logging
import time
from datetime import datetime, timezone

from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import text

from src.db.database import get_session_factory
from src.features import FEATURE_COLUMNS, ZERO_AS_MISSING_COLUMNS
from src.model_health import (
    MIN_DRIFT_SAMPLES,
    PSI_THRESHOLD,
    TARGET_THRESHOLD,
    read_model_health_snapshots,
)

logger = logging.getLogger(__name__)


class ModelHealthCollector:
    def describe(self):
        return []

    def collect(self):
        try:
            with get_session_factory()() as db:
                db.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
                db.execute(text("SET LOCAL statement_timeout = '5s'"))
                snapshots = read_model_health_snapshots(db)
        except Exception:
            logger.error(
                "Не удалось получить месячные агрегаты качества данных и смещения"
            )
            yield GaugeMetricFamily(
                "diabetes_model_health_collection_success",
                "Успех сбора месячных метрик",
                value=0,
            )
            return
        snapshot = snapshots[0]
        scalar = {
            "collection_success": (1, "Успех сбора месячных метрик"),
            "collected_timestamp_seconds": (time.time(), "Время сбора месячных метрик"),
            "studies": (
                snapshot["quality"]["samples"],
                "Исследования за последние 30 дат UTC",
            ),
            "reference_available": (
                int(snapshot["reference"] is not None),
                "Найдена активная версия и её обучающий эталон",
            ),
            "minimum_samples": (
                MIN_DRIFT_SAMPLES,
                "Минимум наблюдений для оценки смещения",
            ),
            "psi_threshold": (
                PSI_THRESHOLD,
                "Рабочий порог PSI, не статистическая гарантия",
            ),
            "target_threshold": (
                TARGET_THRESHOLD,
                "Рабочий порог изменения доли фактических исходов",
            ),
        }
        for bound in ("start", "end"):
            scalar["window_" + bound + "_timestamp_seconds"] = (
                datetime.combine(
                    snapshot[bound], datetime.min.time(), timezone.utc
                ).timestamp(),
                "Граница окна дат исследования UTC; конец не включается",
            )
        for name, (value, description) in scalar.items():
            yield GaugeMetricFamily(
                "diabetes_model_health_" + name, description, value=value
            )
        for kind in ("zeros", "missing"):
            metric = GaugeMetricFamily(
                "diabetes_model_health_feature_" + kind,
                "Количество отсутствующих замеров, закодированных нулём"
                if kind == "zeros"
                else "Количество NULL (должно быть 0)",
                labels=["feature"],
            )
            features = ZERO_AS_MISSING_COLUMNS if kind == "zeros" else FEATURE_COLUMNS
            for feature in features:
                metric.add_metric([feature], snapshot["quality"][feature + "_" + kind])
            yield metric
        labels = ["version", "dataset_id", "role"]
        for name, description in {
            "reference_samples": "Количество строк train эталона версии",
            "current_samples": "Количество новых исследований за месяц",
            "labeled_samples": "Количество новых исследований с фактическим исходом",
            "positive_rate": "Доля положительных фактических исходов за месяц",
            "reference_positive_rate": "Доля положительных исходов в train эталоне",
            "target_shift": "Изменение доли фактических исходов относительно train",
            "evaluated_samples": "Количество проверенных прогнозов версии за месяц",
            "accuracy": "Доля правильных прогнозов версии за месяц",
        }.items():
            metric = GaugeMetricFamily(
                "diabetes_model_health_" + name,
                description,
                labels=labels,
            )
            for item in snapshots:
                reference = item["reference"]
                if reference is not None:
                    values = [
                        reference["version"],
                        str(reference["dataset_id"]),
                        reference["role"],
                    ]
                    value = (
                        reference["samples"]
                        if name == "reference_samples"
                        else item[
                            {
                                "labeled_samples": "labeled",
                                "evaluated_samples": "evaluated",
                            }.get(name, name)
                        ]
                    )
                    metric.add_metric(values, value)
            yield metric
        metric = GaugeMetricFamily(
            "diabetes_model_health_feature_psi",
            "PSI входного признака за месяц относительно train эталона версии",
            labels=labels + ["feature"],
        )
        for item in snapshots:
            reference = item["reference"]
            if reference is not None:
                values = [
                    reference["version"],
                    str(reference["dataset_id"]),
                    reference["role"],
                ]
                for feature, value in item["drift"].items():
                    metric.add_metric(values + [feature], value)
        yield metric

        classification = GaugeMetricFamily(
            "diabetes_model_health_classification",
            "Качество версии за 30 дней по её прогнозам с фактическим исходом",
            labels=labels + ["metric"],
        )
        matrix = GaugeMetricFamily(
            "diabetes_model_health_confusion_matrix",
            "Месячная матрица ошибок: фактический класс и прогноз",
            labels=labels + ["actual", "predicted"],
        )
        for item in snapshots:
            reference = item["reference"]
            if reference is None:
                continue
            values = [
                reference["version"],
                str(reference["dataset_id"]),
                reference["role"],
            ]
            for name, value in item["classification"].items():
                classification.add_metric(values + [name], value)
            for name, actual, predicted in [
                ("tp", "1", "1"),
                ("tn", "0", "0"),
                ("fp", "0", "1"),
                ("fn", "1", "0"),
            ]:
                matrix.add_metric(values + [actual, predicted], item["confusion"][name])
        yield classification
        yield matrix
