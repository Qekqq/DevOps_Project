"""Метрики и безопасный журнал событий рабочих сервисов.

Идентификатор запроса хранится в журнале, но не в метках Prometheus.
Тело запроса, cookies и параметры URL сюда не передаются.
"""

import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from prometheus_client import REGISTRY, Counter, Gauge, Histogram, start_http_server
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

request_id = ContextVar("request_id", default="")
HTTP_REQUESTS = Counter(
    "diabetes_http_requests",
    "Количество завершённых HTTP-запросов",
    ["method", "route", "status"],
)
HTTP_DURATION = Histogram(
    "diabetes_http_duration_seconds",
    "Время обработки HTTP-запроса до подготовки ответа, секунды",
    ["route"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
INFERENCE = Histogram(
    "diabetes_inference_seconds",
    "Время выполнения модели без загрузки и возврата сохранённого результата, секунды",
    ["role", "operation"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.5, 1, 5),
)
PREDICTIONS = Counter(
    "diabetes_predictions", "Количество успешных ответов API с прогнозом", ["cached"]
)
PREDICTIONS.labels(cached="true")
PREDICTIONS.labels(cached="false")
CONSUMER_CONNECTED = Gauge(
    "diabetes_consumer_connected",
    "Подключение обработчика Kafka: 1 — подключён, 0 — отключён",
)
DELIVERY = Histogram(
    "diabetes_delivery_seconds",
    "Время от публикации до завершения фоновых прогнозов, секунды",
    buckets=(0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 300),
)


class EventFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "timestamp": datetime.fromtimestamp(
                record.created, timezone.utc
            ).isoformat(),
            "service": os.getenv("APP_SERVICE", "local"),
            "level": record.levelname,
            "action": record.msg,
            "message": {
                "http_request": "Запрос к приложению",
                "request_failed": "Ошибка обработки запроса",
                "login_succeeded": "Вход выполнен",
                "service_started": "Сервис запущен",
                "prediction_returned": "Возвращён сохранённый прогноз",
                "prediction_published": "Прогноз передан на сохранение",
                "study_saved": "Изменения исследования сохранены",
                "feedback_saved": "Обратная связь сохранена",
                "prediction_processing_completed": "Фоновая обработка завершена",
                "duplicate_message_skipped": "Повторная запись пропущена",
                "consumer_failed": "Ошибка фоновой обработки",
                "quality_collection_failed": "Ошибка чтения метрик из БД",
            }.get(record.msg, record.msg),
            "request_id": request_id.get(),
        }
        # Только явно разрешённые скалярные поля. Никакого str(exception).
        for key in (
            "route",
            "method",
            "status",
            "duration_ms",
            "actor_id",
            "error_type",
            "cached",
            "recalculated",
            "feedback_changed",
        ):
            value = getattr(record, key, None)
            if isinstance(value, (str, int, float, bool)):
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False)


def event(action, *, level=logging.INFO, **fields):
    logger = logging.getLogger("diabetes.events")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(EventFormatter())
        logger.addHandler(handler)
        if os.getenv("APP_SERVICE"):
            directory = Path(os.getenv("APP_LOG_DIR", "/app/logs"))
            directory.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                directory / f"{os.environ['APP_SERVICE']}.jsonl",
                maxBytes=5_000_000,
                backupCount=2,
                encoding="utf-8",
            )
            file_handler.setFormatter(EventFormatter())
            logger.addHandler(file_handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    logger.log(level, action, extra=fields)


@contextmanager
def measure_inference(role, operation):
    with INFERENCE.labels(role=role, operation=operation).time():
        yield


class ContainerCollector:
    """Cgroup v2 своего контейнера: не требует доступа к Docker socket."""

    def collect(self):
        root = Path("/sys/fs/cgroup")
        try:
            cpu = dict(
                line.split() for line in (root / "cpu.stat").read_text().splitlines()
            )
            memory = int((root / "memory.current").read_text())
        except (OSError, ValueError):
            return
        yield CounterMetricFamily(
            "diabetes_container_cpu_seconds",
            "Накопленное процессорное время контейнера, секунды",
            value=int(cpu["usage_usec"]) / 1e6,
        )
        yield GaugeMetricFamily(
            "diabetes_container_memory_bytes",
            "Память контейнера, включая файловый кеш, байты",
            value=memory,
        )


def start_metrics():
    if not os.getenv("METRICS_PORT"):
        return None
    REGISTRY.register(ContainerCollector())
    return start_http_server(int(os.environ["METRICS_PORT"]))


@asynccontextmanager
async def lifespan(app):
    server = start_metrics()
    event("service_started")
    try:
        yield
    finally:
        if server:
            server[0].shutdown()
            server[0].server_close()
            server[1].join()


async def observe_request(request, call_next):
    token = request_id.set(uuid.uuid4().hex)
    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = request_id.get()
        return response
    except Exception as error:
        event("request_failed", level=logging.ERROR, error_type=type(error).__name__)
        raise
    finally:
        route = getattr(request.scope.get("route"), "path", "unmatched")
        elapsed = time.perf_counter() - started
        if route not in {"/health", "/db/health", "/monitoring/access"}:
            method = (
                request.method
                if request.method
                in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
                else "OTHER"
            )
            HTTP_REQUESTS.labels(method, route, str(status)).inc()
            HTTP_DURATION.labels(route).observe(elapsed)
            session = getattr(request.state, "auth_session", None)
            event(
                "http_request",
                level=logging.ERROR
                if status >= 500
                else (logging.WARNING if status >= 400 else logging.INFO),
                method=method,
                route=route,
                status=status,
                duration_ms=round(elapsed * 1000, 2),
                actor_id=getattr(session, "user_id", None),
            )
        request_id.reset(token)
