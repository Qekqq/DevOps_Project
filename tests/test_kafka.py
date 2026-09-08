import pytest
from types import SimpleNamespace
from unittest.mock import Mock
from datetime import date
from kafka.errors import NoBrokersAvailable

import src.kafka.consumer as consumer_module
import src.kafka.producer as producer_module


KAFKA_SETTINGS = {
    "KAFKA_BOOTSTRAP_SERVERS": "kafka:9092",
    "KAFKA_PREDICTION_TOPIC": "prediction-results",
    "KAFKA_CONSUMER_GROUP": "prediction-results-consumer",
}


VALID_MESSAGE = {
    "study_date": "2026-09-08",
    "model_version": "lab2-1.0.0",
    "patient_code": "TEST-001",
    "features": {"glucose": 148, "bmi": 33.6},
    "prediction": 1,
    "probability": 0.81,
    "label": "detected",
    "request_source": "api",
    "response_time_ms": 12,
}


class FakeProducer:
    def __init__(self) -> None:
        self.sent = []
        self.flushed = False

    def send(self, topic, value=None, key=None) -> None:
        self.sent.append((topic, value, key))
        return self

    def get(self, timeout=None):
        self.flushed = True

    def flush(self, timeout=None) -> None:
        self.flushed = True


class FakeDb:
    def __init__(self) -> None:
        self.committed = False

    def __enter__(self) -> "FakeDb":
        return self

    def __exit__(self, *args) -> bool:
        return False

    def commit(self) -> None:
        self.committed = True


@pytest.fixture(autouse=True)
def reset_producer_singleton():
    producer_module._producer = None
    yield
    producer_module._producer = None


def test_send_prediction_message_publishes_to_topic(monkeypatch):
    fake_producer = FakeProducer()

    monkeypatch.setattr(producer_module, "get_kafka_secrets", lambda: KAFKA_SETTINGS)
    monkeypatch.setattr(producer_module, "get_producer", lambda: fake_producer)

    producer_module.send_prediction_message(VALID_MESSAGE, key="TEST-001")

    assert fake_producer.sent == [("prediction-results", VALID_MESSAGE, "TEST-001")]
    assert fake_producer.flushed is True


def test_get_producer_uses_bootstrap_servers_from_vault(monkeypatch):
    captured = {}

    class FakeKafkaProducer:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(producer_module, "KafkaProducer", FakeKafkaProducer)
    monkeypatch.setattr(producer_module, "get_kafka_secrets", lambda: KAFKA_SETTINGS)

    first = producer_module.get_producer()
    second = producer_module.get_producer()

    assert captured["bootstrap_servers"] == "kafka:9092"
    assert first is second


def test_save_message_to_database_persists_prediction(monkeypatch):
    fake_db = FakeDb()
    captured = {}

    monkeypatch.setattr(consumer_module, "get_session_factory", lambda: (lambda: fake_db))
    def lookup_model(db, version):
        assert version == "lab2-1.0.0"
        return "message-model"

    monkeypatch.setattr(consumer_module, "require_model_version", lookup_model)
    monkeypatch.setattr(
        consumer_module,
        "save_prediction_history",
        lambda **kwargs: captured.update(kwargs),
    )

    consumer_module.save_message_to_database(VALID_MESSAGE)

    assert captured["prediction"] == 1
    assert captured["features"] == {"glucose": 148, "bmi": 33.6}
    assert captured["model_version"] == "message-model"
    assert captured["study_date"] == date(2026, 9, 8)
    assert captured["patient_code"] == "TEST-001"
    assert fake_db.committed is True


def test_create_consumer_retries_until_broker_available(monkeypatch):
    attempts = {"count": 0}
    sentinel_consumer = object()

    def fake_kafka_consumer(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise NoBrokersAvailable()
        return sentinel_consumer

    monkeypatch.setattr(consumer_module, "get_kafka_secrets", lambda: KAFKA_SETTINGS)
    monkeypatch.setattr(consumer_module, "KafkaConsumer", fake_kafka_consumer)
    monkeypatch.setattr(consumer_module.time, "sleep", lambda seconds: None)

    consumer = consumer_module.create_consumer()

    assert consumer is sentinel_consumer
    assert attempts["count"] == 3


@pytest.mark.parametrize("fails", [False, True])
def test_consumer_acknowledges_only_successful_database_write(monkeypatch, fails):
    record = SimpleNamespace(value=VALID_MESSAGE, topic="predictions", partition=0, offset=8)
    class Consumer:
        commit = Mock()
        close = Mock()
        def __iter__(self):
            return iter([record])
    consumer = Consumer()
    monkeypatch.setattr(consumer_module, "create_consumer", lambda: consumer)
    monkeypatch.setattr(consumer_module, "predict_challengers", Mock())
    save = Mock(side_effect=RuntimeError("database unavailable") if fails else None)
    monkeypatch.setattr(consumer_module, "save_message_to_database", save)
    if fails:
        with pytest.raises(RuntimeError):
            consumer_module.run()
        consumer.commit.assert_not_called()
    else:
        consumer_module.run()
        offsets = consumer.commit.call_args.args[0]
        assert next(iter(offsets.values())).offset == 9
    consumer.close.assert_called_once()
