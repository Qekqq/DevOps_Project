"""Reviewed Apache Kafka settings for new releases; never applied to old volumes here."""

SERVICES = {"db", "vault", "kafka"}
KAFKA_ENV = {
    "KAFKA_NODE_ID": "1",
    "KAFKA_PROCESS_ROLES": "broker,controller",
    "KAFKA_CONTROLLER_QUORUM_VOTERS": "1@kafka:9093",
    "KAFKA_LISTENERS": "PLAINTEXT://:9092,CONTROLLER://:9093",
    "KAFKA_ADVERTISED_LISTENERS": "PLAINTEXT://kafka:9092",
    "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP": "CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT",
    "KAFKA_CONTROLLER_LISTENER_NAMES": "CONTROLLER",
    "KAFKA_INTER_BROKER_LISTENER_NAME": "PLAINTEXT",
    "KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR": "1",
    "KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR": "1",
    "KAFKA_TRANSACTION_STATE_LOG_MIN_ISR": "1",
    "KAFKA_LOG_DIRS": "/bitnami/kafka/data",
    "KAFKA_HEAP_OPTS": "-Xms256m -Xmx512m",
    # Existing installations must preserve and verify their own cluster ID.
    "CLUSTER_ID": "9SpKIAuUSqaa0BehE-bzsg",
}


def apply_images(config, images):
    if set(images) != SERVICES:
        raise ValueError("All three infrastructure image digests are required")
    for name, image in images.items():
        config["services"][name]["image"] = image
    kafka = config["services"]["kafka"]
    kafka["environment"] = dict(KAFKA_ENV)
    kafka["healthcheck"]["test"] = [
        "CMD-SHELL",
        "/opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --list >/dev/null 2>&1",
    ]
