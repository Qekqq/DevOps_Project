import json
import time
from datetime import date
import re

from kafka import KafkaConsumer
from kafka.errors import NoBrokersAvailable
from sqlalchemy.exc import SQLAlchemyError

from src.db.database import get_session_factory
from src.db.repositories import (
    DuplicatePredictionError,
    require_model_version,
    save_prediction_history,
)
from src.logger import get_logger
from src.secrets.vault_client import get_kafka_secrets


logger = get_logger(__name__)


def save_message_to_database(message: dict) -> None:
    """
    Сохраняет результат работы модели из Kafka в PostgreSQL.

    Параметры подключения к БД берутся из Vault (внутри get_session_factory).
    """
    study_date_text = message["study_date"]
    if not isinstance(study_date_text, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", study_date_text):
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
            request_source=message.get("request_source", "api"),
            response_time_ms=message.get("response_time_ms"),
        )

        db.commit()


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
                enable_auto_commit=True,
            )

            logger.info("Kafka consumer connected.")
            return consumer

        except NoBrokersAvailable:
            logger.warning("Kafka broker недоступен (попытка %s). Повтор...", attempt)
            time.sleep(3)

    raise RuntimeError("Не удалось подключиться к брокеру Kafka")


def run() -> None:
    """
    Запускает бесконечный цикл приёма сообщений из Kafka.
    """
    logger.info("Starting Kafka consumer service...")
    consumer = create_consumer()

    for record in consumer:
        message = record.value

        try:
            save_message_to_database(message)
            logger.info(
                "Прогноз из Kafka сохранён в БД (patient=%s).",
                message.get("patient_code"),
            )
        except DuplicatePredictionError:
            logger.info("Повторный прогноз пропущен (patient=%s).", message.get("patient_code"))
        except (KeyError, ValueError, RuntimeError, SQLAlchemyError) as error:
            logger.error("Не удалось сохранить сообщение из Kafka в БД: %s", error)


if __name__ == "__main__":
    run()
