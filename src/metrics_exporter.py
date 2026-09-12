"""Внутренний exporter: только агрегаты, без кодов пациентов и входных показателей."""

import threading

from prometheus_client import CollectorRegistry, start_http_server

from src.runtime_logging import configure_diagnostics
from src.telemetry import ContainerCollector


def main():
    configure_diagnostics()
    registry = CollectorRegistry()
    # Качество моделей рассчитывается только по запросу в /monitoring/model-report.
    # Здесь остаются технические показатели процесса, без опроса БД.
    registry.register(ContainerCollector())
    start_http_server(9100, registry=registry)
    threading.Event().wait()


if __name__ == "__main__":
    main()
