import pytest

from scripts.infrastructure_release import KAFKA_ENV, apply_images


def test_release_replaces_bitnami_settings_and_keeps_mounts():
    services = {name: {"image": "old"} for name in ("db", "vault", "kafka")}
    services["kafka"].update(
        environment={"KAFKA_CFG_NODE_ID": "1"},
        healthcheck={"test": ["old"], "interval": "5s"},
        volumes=["kafka_data:/bitnami/kafka"],
    )
    images = {
        name: "example/repository@sha256:" + str(i) * 64
        for i, name in enumerate(services)
    }
    apply_images({"services": services}, images)
    assert {name: value["image"] for name, value in services.items()} == images
    assert services["kafka"]["environment"] == KAFKA_ENV
    assert services["kafka"]["volumes"] == ["kafka_data:/bitnami/kafka"]
    assert services["kafka"]["healthcheck"]["interval"] == "5s"
    assert "/opt/kafka/bin/" in services["kafka"]["healthcheck"]["test"][1]


def test_incomplete_infrastructure_release_is_rejected():
    with pytest.raises(ValueError):
        apply_images({}, {"db": "image"})
