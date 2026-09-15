import json
import logging
import re
import time
from datetime import date, datetime, timezone

from kafka.errors import NoBrokersAvailable
from kafka.structs import OffsetAndMetadata

from kafka import KafkaConsumer, TopicPartition
from src.db.database import get_session_factory
from src.db.repositories import (
    DuplicatePredictionError,
    StudyConflictError,
    require_model_version,
    save_prediction_history,
)
from src.kafka.retries import defer_predictions, start_retry_worker
from src.kafka.shadow import ShadowPredictionError, predict_challengers
from src.logger import get_logger
from src.runtime_logging import configure_diagnostics
from src.secrets.vault_client import get_kafka_secrets
from src.telemetry import CONSUMER_CONNECTED, DELIVERY, event, request_id, start_metrics

logger = get_logger(__name__)


def consumer_connected(consumer):
    # bootstrap_connected() проверяет только начальное
    # соединение, которое закрывается после получения metadata. Проверяем
    # рабочие соединения клиента; публичного аналога у этой версии нет.
    return any(conn.connected() for conn in list(consumer._client._conns.values()))


def save_message_to_database(message: dict) -> None:
    """
    Сохраняет результат работы модели из Kafka в PostgreSQL.

    Параметры подключения к БД берутся из Vault (внутри get_session_factory).
    """
    study_date_text = message["study_date"]
    if not isinstance(study_date_text, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}", study_date_text
    ):
        raise ValueError("Укажите дату исследования в формате ГГГГ-ММ-ДД")
    study_date = date.fromisoformat(study_date_text)
    session_factory = get_session_factory()

    with session_factory() as db:
        model_version = require_model_version(db, message["model_version"])

        save_prediction_history(
            db=db,
            features=message["features"],
            prediction=message["prediction"],
            probability=message.get("probability"),
            model_version=model_version,
            study_date=study_date,
            patient_code=message.get("patient_code"),
            response_time_ms=message.get("response_time_ms"),
            # В старых сообщениях поле отсутствует: producer публиковал champion.
            role_at_prediction=message.get("role_at_prediction", "champion"),
            predicted_at=prediction_time(message),
        )

        db.commit()


def prediction_time(message):
    """Сохраняем время события, а не время доставки Kafka. Старые сообщения — fallback БД."""
    value = message.get("created_at")
    if value is None:
        return None
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Время прогноза должно содержать часовой пояс")
    return result.astimezone(timezone.utc)


def create_consumer() -> KafkaConsumer:
    """
    Создаёт Kafka Consumer с повторными попытками подключения к брокеру.
    """
    kafka_settings = get_kafka_secrets()

    for attempt in range(1, 31):
        try:
            consumer = KafkaConsumer(
                kafka_settings["KAFKA_PREDICTION_TOPIC"],
                bootstrap_servers=kafka_settings["KAFKA_BOOTSTRAP_SERVERS"],
                group_id=kafka_settings["KAFKA_CONSUMER_GROUP"],
                value_deserializer=lambda value: json.loads(value.decode("utf-8")),
                auto_offset_reset="earliest",
                enable_auto_commit=False,
                max_poll_records=1,
            )

            logger.info("Kafka consumer подключён.")
            CONSUMER_CONNECTED.set_function(lambda: consumer_connected(consumer))
            return consumer

        except NoBrokersAvailable:
            logger.warning("Kafka broker недоступен (попытка %s). Повтор...", attempt)
            time.sleep(3)

    raise RuntimeError("Не удалось подключиться к брокеру Kafka")


def run() -> None:
    """
    Запускает бесконечный цикл приёма сообщений из Kafka.
    """
    logger.info("Запуск Kafka consumer...")
    consumer = create_consumer()

    try:
        for record in consumer:
            message = record.value
            deferred = False
            correlation = message.get("request_id", "")
            request_id.set(
                correlation
                if isinstance(correlation, str)
                and re.fullmatch(r"[a-f0-9]{32}", correlation)
                else ""
            )
            try:
                save_message_to_database(message)
            except DuplicatePredictionError:
                event("duplicate_message_skipped")
            except StudyConflictError:
                logger.info(
                    "Пропущено сообщение с показателями до исправления исследования"
                )
                message = None
            if message is not None:
                try:
                    predict_challengers(message)
                except ShadowPredictionError as error:
                    defer_predictions(message, error.versions)
                    deferred = True
            # Основной прогноз и задачи повторов уже в БД. Если БД недоступна,
            # исключение прервёт обработку без подтверждения offset.
            consumer.commit(
                {
                    TopicPartition(record.topic, record.partition): OffsetAndMetadata(
                        record.offset + 1, "", getattr(record, "leader_epoch", -1)
                    )
                }
            )
            if not deferred:
                event("prediction_processing_completed")
            try:
                published = datetime.fromisoformat(
                    (message or {}).get("created_at", "") if not deferred else ""
                )
                DELIVERY.observe(
                    max(0, (datetime.now(timezone.utc) - published).total_seconds())
                )
            except (ValueError, TypeError):
                pass  # Старые сообщения могли не содержать время публикации.
            request_id.set("")
    except Exception as error:
        event("consumer_failed", level=logging.ERROR, error_type=type(error).__name__)
        raise
    finally:
        consumer.close()


if __name__ == "__main__":
    configure_diagnostics()
    start_metrics()
    retry_stop = start_retry_worker()
    try:
        run()
    finally:
        retry_stop.set()
