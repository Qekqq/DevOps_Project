"""Внутренний exporter: только агрегаты, без кодов пациентов и входных показателей."""

import logging
import threading
import time

from prometheus_client import CollectorRegistry, start_http_server
from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import text

from src.db.database import get_session_factory
from src.model_health_exporter import ModelHealthCollector
from src.monitoring import read_quality_snapshot
from src.telemetry import ContainerCollector, event

logger = logging.getLogger(__name__)


class QualityCollector:
    def describe(self):
        return []

    def collect(self):
        started = time.monotonic()
        try:
            with get_session_factory()() as db:
                db.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
                db.execute(text("SET LOCAL statement_timeout = '5s'"))
                snapshot = read_quality_snapshot(db)
        except Exception:
            event("quality_collection_failed", level=logging.ERROR)
            logger.error("Не удалось получить агрегаты качества из БД")
            yield GaugeMetricFamily(
                "diabetes_quality_collection_success",
                "Успешность чтения метрик качества из БД: 1 — успешно, 0 — ошибка",
                value=0,
            )
            return
        yield GaugeMetricFamily(
            "diabetes_quality_collection_success",
            "Успешность чтения метрик качества из БД: 1 — успешно, 0 — ошибка",
            value=1,
        )
        yield GaugeMetricFamily(
            "diabetes_quality_collected_timestamp_seconds",
            "Время последнего успешного чтения метрик качества, Unix-время в секундах",
            value=time.time(),
        )
        yield GaugeMetricFamily(
            "diabetes_quality_collection_duration_seconds",
            "Продолжительность чтения метрик качества, секунды",
            value=time.monotonic() - started,
        )
        descriptions = {
            "studies": "Количество исследований",
            "feedback": "Количество исследований с фактическим исходом",
            "cohort": "Количество исследований в общей выборке оценки активных моделей",
        }
        for name, description in descriptions.items():
            yield GaugeMetricFamily(
                f"diabetes_{name}", description, value=snapshot[name]
            )
        yield GaugeMetricFamily(
            "diabetes_incomplete_studies",
            "Исследования без результатов хотя бы одной активной модели",
            value=snapshot.get("pending", 0),
        )
        labels = ["model", "version", "role"]
        report = GaugeMetricFamily(
            "diabetes_classification_report",
            "Отчёт sklearn о качестве классификации по фактическим исходам",
            labels=labels + ["class", "metric"],
        )
        matrix = GaugeMetricFamily(
            "diabetes_confusion_matrix",
            "Матрица ошибок: строки — фактический класс, столбцы — прогноз",
            labels=labels + ["actual", "predicted"],
        )
        for model in snapshot["models"]:
            values = [model["name"], model["version"], model["role"]]
            for outcome, (actual, predicted) in {
                "tn": ("0", "0"),
                "fp": ("0", "1"),
                "fn": ("1", "0"),
                "tp": ("1", "1"),
            }.items():
                matrix.add_metric(
                    values + [actual, predicted], model["counts"][outcome]
                )
            for name, row in model["report"].items():
                if isinstance(row, dict):
                    for metric, value in row.items():
                        report.add_metric(values + [name, metric], value)
                else:
                    report.add_metric(values + [name, "f1-score"], row)
                    report.add_metric(
                        values + [name, "support"], sum(model["counts"].values())
                    )
        yield report
        yield matrix


def main():
    registry = CollectorRegistry()
    registry.register(QualityCollector())
    registry.register(ModelHealthCollector())
    registry.register(ContainerCollector())
    start_http_server(9100, registry=registry)
    threading.Event().wait()


if __name__ == "__main__":
    main()
