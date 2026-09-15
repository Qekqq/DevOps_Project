"""Rehearse a Kafka KRaft upgrade using synthetic messages and cloned test data."""

import argparse
import json
import subprocess
import time
from uuid import uuid4

from scripts.volume_snapshot import (
    SnapshotError,
    create_snapshot,
    restore_snapshot_to_new_volume,
)

COMMON = {
    "NODE_ID": "1",
    "PROCESS_ROLES": "broker,controller",
    "CONTROLLER_QUORUM_VOTERS": "1@kafka:9093",
    "LISTENERS": "PLAINTEXT://:9092,CONTROLLER://:9093",
    "ADVERTISED_LISTENERS": "PLAINTEXT://kafka:9092",
    "LISTENER_SECURITY_PROTOCOL_MAP": "CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT",
    "CONTROLLER_LISTENER_NAMES": "CONTROLLER",
    "INTER_BROKER_LISTENER_NAME": "PLAINTEXT",
    "OFFSETS_TOPIC_REPLICATION_FACTOR": "1",
    "TRANSACTION_STATE_LOG_REPLICATION_FACTOR": "1",
    "TRANSACTION_STATE_LOG_MIN_ISR": "1",
}


def check_upgrade(old_image, new_image, client_image):
    label = "devops-kafka-upgrade-" + uuid4().hex[:12]
    containers, volumes = [], []
    network = None

    def docker(*args):
        result = subprocess.run(
            ["docker", *args], text=True, encoding="utf-8", capture_output=True
        )
        if result.returncode:
            raise RuntimeError(
                f"Isolated Kafka check failed during {args[0]}: {result.stderr[-1500:]}"
            )
        return result.stdout.strip()

    def ready(name, binary):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    name,
                    binary + "/kafka-topics.sh",
                    "--bootstrap-server",
                    "localhost:9092",
                    "--list",
                ],
                capture_output=True,
                timeout=20,
            )
            if result.returncode == 0:
                return
            time.sleep(2)
        raise RuntimeError(
            "Test Kafka did not become ready: " + docker("logs", "--tail", "12", name)
        )

    def start(image, volume, suffix, apache=False, cluster_id=None):
        name = label + "-" + suffix
        args = [
            "run",
            "-d",
            "--name",
            name,
            "--label",
            "devops.test=" + label,
            "--network",
            network,
            "--network-alias",
            "kafka",
            "--memory",
            "1536m",
            "--cpus",
            "1",
            "--env",
            "KAFKA_HEAP_OPTS=-Xms256m -Xmx512m",
            "--mount",
            f"type=volume,source={volume},target=/bitnami/kafka",
        ]
        prefix = "KAFKA_" if apache else "KAFKA_CFG_"
        for key, value in COMMON.items():
            args += ["--env", prefix + key + "=" + value]
        if apache:
            args += [
                "--env",
                "KAFKA_LOG_DIRS=/bitnami/kafka/data",
                "--env",
                "CLUSTER_ID=" + cluster_id,
            ]
        else:
            args += ["--env", "ALLOW_PLAINTEXT_LISTENER=yes"]
        docker(*args, image)
        containers.append(name)
        ready(name, "/opt/kafka/bin" if apache else "/opt/bitnami/kafka/bin")
        return name

    def client(code):
        return docker(
            "run",
            "--rm",
            "--network",
            network,
            "--memory",
            "256m",
            "--cpus",
            "1",
            "--read-only",
            "--cap-drop",
            "ALL",
            client_image,
            "python",
            "-c",
            code,
        )

    preamble = """
from kafka import KafkaProducer, KafkaConsumer, TopicPartition
from kafka.admin import KafkaAdminClient, NewTopic
topic='upgrade-probe'
"""
    try:
        old_id = json.loads(docker("image", "inspect", old_image))[0]["Id"]
        new_id = json.loads(docker("image", "inspect", new_image))[0]["Id"]
        client_image = json.loads(docker("image", "inspect", client_image))[0]["Id"]
        network = docker(
            "network", "create", "--internal", "--label", "devops.test=" + label, label
        )
        for suffix in ("original",):
            volume = label + "-" + suffix
            docker(
                "volume",
                "create",
                "--label",
                "devops.test=" + label,
                "--label",
                "com.docker.compose.project=" + label,
                "--label",
                "com.docker.compose.volume=kafka_data",
                volume,
            )
            volumes.append(volume)
        old = start(old_id, volumes[0], "old")
        properties = docker("exec", old, "cat", "/bitnami/kafka/data/meta.properties")
        cluster_id = dict(
            line.split("=", 1) for line in properties.splitlines() if "=" in line
        )["cluster.id"]
        old_features = docker(
            "exec",
            old,
            "/opt/bitnami/kafka/bin/kafka-features.sh",
            "--bootstrap-server",
            "localhost:9092",
            "describe",
        )
        client(
            preamble
            + """
admin=KafkaAdminClient(bootstrap_servers='kafka:9092')
admin.create_topics([NewTopic(topic,1,1)]); admin.close()
p=KafkaProducer(bootstrap_servers='kafka:9092')
assert p.send(topic,b'first',partition=0).get(timeout=20).offset==0
c=KafkaConsumer(topic,bootstrap_servers='kafka:9092',group_id='upgrade-group',auto_offset_reset='earliest',enable_auto_commit=False,consumer_timeout_ms=20000,max_poll_records=1)
assert next(c).value==b'first'; c.commit(); c.close()
assert p.send(topic,b'second',partition=0).get(timeout=20).offset==1; p.close()
"""
        )
        docker("stop", "--time", "30", old)
        docker("network", "disconnect", network, old)
        try:
            snapshot = create_snapshot(label, "kafka", volumes[0], client_image)
            volumes.append(snapshot["volume"])
            restored = restore_snapshot_to_new_volume(snapshot)
            volumes.append(restored)
        except SnapshotError as error:
            if error.volume and error.volume not in volumes:
                volumes.append(error.volume)
            raise
        docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "0:0",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "CHOWN",
            "--cap-add",
            "DAC_OVERRIDE",
            "--cap-add",
            "FOWNER",
            "--mount",
            f"type=volume,source={restored},target=/destination",
            "--entrypoint",
            "bash",
            old_id,
            "-o",
            "pipefail",
            "-c",
            "chown -hR 1000:1000 /destination",
        )
        new = start(new_id, restored, "new", apache=True, cluster_id=cluster_id)
        client(
            preamble
            + """
c=KafkaConsumer(topic,bootstrap_servers='kafka:9092',group_id='upgrade-group',enable_auto_commit=False,consumer_timeout_ms=20000,max_poll_records=1)
assert c.committed(TopicPartition(topic,0))==1
record=next(c); assert (record.offset,record.value)==(1,b'second'); c.commit(); c.close()
p=KafkaProducer(bootstrap_servers='kafka:9092'); assert p.send(topic,b'third',partition=0).get(timeout=20).offset==2; p.close()
"""
        )
        docker("restart", new)
        ready(new, "/opt/kafka/bin")
        client(
            preamble
            + """
c=KafkaConsumer(topic,bootstrap_servers='kafka:9092',group_id='upgrade-group',enable_auto_commit=False,consumer_timeout_ms=20000)
assert c.committed(TopicPartition(topic,0))==2
record=next(c); assert (record.offset,record.value)==(2,b'third'); c.close()
"""
        )
        new_features = docker(
            "exec",
            new,
            "/opt/kafka/bin/kafka-features.sh",
            "--bootstrap-server",
            "localhost:9092",
            "describe",
        )
        docker("stop", "--time", "30", new)
        docker("network", "disconnect", network, new)
        docker("network", "connect", "--alias", "kafka", network, old)
        docker("start", old)
        ready(old, "/opt/bitnami/kafka/bin")
        client(
            preamble
            + """
c=KafkaConsumer(bootstrap_servers='kafka:9092',group_id='upgrade-group',enable_auto_commit=False)
t=TopicPartition(topic,0); assert c.committed(t)==1; assert c.end_offsets([t])[t]==2; c.close()
"""
        )
        print(
            json.dumps(
                {
                    "messages_preserved": True,
                    "group_offset_preserved": True,
                    "restart_verified": True,
                    "original_recovered": True,
                    "old_features": old_features,
                    "new_features": new_features,
                }
            )
        )
    finally:
        for name in reversed(containers):
            docker("rm", "-f", "-v", name)
        for name in reversed(volumes):
            docker("volume", "rm", name)
        if network:
            docker("network", "rm", network)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-image", required=True)
    parser.add_argument("--new-image", required=True)
    parser.add_argument("--client-image", required=True)
    args = parser.parse_args()
    check_upgrade(args.old_image, args.new_image, args.client_image)
