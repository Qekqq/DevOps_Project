"""Freeze local application writers and prepare a verified cutover data export."""

import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path

from scripts.backup_database import create_database_backup
from scripts.deploy_release import existing_services
from scripts.vault_connection import host_bind_source


def docker(*args):
    return subprocess.check_output(
        ["docker", *args], text=True, encoding="utf-8", stderr=subprocess.PIPE
    )


def counts(container, *, user="diabetes", database="diabetes", remote=None):
    run = docker if remote is None else remote.docker

    def query(sql):
        result = run(
            "exec",
            container,
            "psql",
            "-U",
            user,
            "-d",
            database,
            "-At",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            sql,
        )
        return result.decode().strip() if isinstance(result, bytes) else result.strip()

    tables = query(
        "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
    ).splitlines()
    result = {}
    for table in tables:
        if not table.replace("_", "").isalnum():
            raise ValueError("Unexpected table name")
        result[table] = int(query('SELECT count(*) FROM public."' + table + '"'))
    return result


def freeze_export(folder):
    folder = folder.resolve()
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    services = existing_services()
    writers = ["frontend", "diabetes-api", "kafka-consumer", "metrics-exporter"]
    state = {
        "format": 1,
        "kind": "cutover",
        "writers": [
            services[n]["Id"] for n in writers if services[n]["State"]["Running"]
        ],
        "ready": False,
    }
    state_path = folder / "source.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    # Capture nonsecret queue identifiers before stopping the API.
    queue = json.loads(
        docker(
            "exec",
            services["diabetes-api"]["Id"],
            "python",
            "-c",
            "import json; from src.secrets.vault_client import get_kafka_secrets; p=get_kafka_secrets(); print(json.dumps({k:p[k] for k in ('KAFKA_PREDICTION_TOPIC','KAFKA_CONSUMER_GROUP')}))",
        )
    )
    ingress = [
        services[n]["Id"]
        for n in ("frontend", "diabetes-api")
        if services[n]["State"]["Running"]
    ]
    if ingress:
        try:
            docker("stop", "--time", "60", *ingress)
        except BaseException:
            resume_source(folder)
            raise
    try:
        for _ in range(60):
            output = docker(
                "exec",
                services["kafka"]["Id"],
                "/opt/bitnami/kafka/bin/kafka-consumer-groups.sh",
                "--bootstrap-server",
                "localhost:9092",
                "--group",
                queue["KAFKA_CONSUMER_GROUP"],
                "--describe",
            )
            rows = [
                line.split()
                for line in output.splitlines()
                if queue["KAFKA_PREDICTION_TOPIC"] in line.split()
            ]
            if rows and all(len(row) >= 6 and row[5] == "0" for row in rows):
                break
            time.sleep(2)
        else:
            raise RuntimeError("Kafka queue is not drained; transfer cancelled")
        remaining = [
            services[n]["Id"]
            for n in ("kafka-consumer", "metrics-exporter")
            if services[n]["State"]["Running"]
        ]
        if remaining:
            docker("stop", "--time", "60", *remaining)
        env = dict(v.split("=", 1) for v in services["db"]["Config"]["Env"] if "=" in v)
        state["tables"] = counts(
            services["db"]["Id"], user=env["POSTGRES_USER"], database=env["POSTGRES_DB"]
        )
        backup = create_database_backup(folder, container=services["db"]["Id"])
        state["backup"] = backup.name
        state["backup_sha256"] = hashlib.sha256(backup.read_bytes()).hexdigest()
        mount = next(
            m
            for m in services["diabetes-api"]["Mounts"]
            if m["Destination"] == "/app/data/feedback"
        )
        if mount["Type"] != "bind":
            raise ValueError("Unsupported feedback storage")
        feedback = Path(host_bind_source(mount["Source"]))
        if any(p.is_symlink() for p in feedback.rglob("*")):
            raise ValueError("Feedback export must not follow links")
        shutil.copytree(feedback, folder / "feedback")
        state["feedback_files"] = {
            p.relative_to(folder / "feedback").as_posix(): hashlib.sha256(
                p.read_bytes()
            ).hexdigest()
            for p in (folder / "feedback").rglob("*")
            if p.is_file()
        }
        state["ready"] = True
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        return backup, folder / "feedback", state["tables"]
    except BaseException:
        resume_source(folder)
        raise


def resume_source(folder):
    state = json.loads((Path(folder) / "source.json").read_text(encoding="utf-8"))
    services = existing_services()
    allowed = {
        v["Id"]
        for n, v in services.items()
        if n in ("frontend", "diabetes-api", "kafka-consumer", "metrics-exporter")
    }
    if not set(state["writers"]).issubset(allowed):
        raise RuntimeError("Source containers changed; refusing automatic restart")
    if state["writers"]:
        docker("start", *state["writers"])
