import json

from kafka import KafkaProducer

from src.logger import get_logger
from src.secrets.vault_client import get_kafka_secrets


logger = get_logger(__name__)

_producer: KafkaProducer | None = None


def get_producer() -> KafkaProducer:
    """
    Создаёт (один раз) и возвращает Kafka Producer.

    Настройки подключения берутся из Hashicorp Vault.
    """
    global _producer

    if _producer is None:
        kafka_settings = get_kafka_secrets()

        _producer = KafkaProducer(
            bootstrap_servers=kafka_settings["KAFKA_BOOTSTRAP_SERVERS"],
            value_serializer=lambda value: json.dumps(value).encode("utf-8"),
            key_serializer=lambda key: key.encode("utf-8") if key else None,
            acks="all",
            retries=3,
        )

        logger.info("Kafka producer initialized.")

    return _producer


def send_prediction_message(message: dict, key: str | None = None) -> None:
    """
    Публикует сообщение с результатом работы модели в топик Kafka.
    """
    kafka_settings = get_kafka_secrets()
    topic = kafka_settings["KAFKA_PREDICTION_TOPIC"]

    producer = get_producer()
    producer.send(topic, value=message, key=key).get(timeout=10)

    logger.info("Prediction message published to topic '%s'.", topic)
