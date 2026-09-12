"""Test an image upgrade on synthetic data and isolated, automatically named volumes."""

import argparse
import json
import os
import secrets
import subprocess
import time
from uuid import uuid4

from scripts.volume_snapshot import (
    SnapshotError,
    create_snapshot,
    restore_snapshot_to_new_volume,
)


def check_upgrade(old_image, new_image, helper_image):
    label = "devops-pg-upgrade-" + uuid4().hex[:12]
    containers, volumes = [], []
    env = dict(os.environ, POSTGRES_PASSWORD=secrets.token_hex(32))

    def docker(*args, stdin=None):
        result = subprocess.run(
            ["docker", *args],
            input=stdin,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if result.returncode:
            diagnostic = result.stderr.replace(env["POSTGRES_PASSWORD"], "<redacted>")[
                -1500:
            ]
            raise RuntimeError(
                f"Isolated PostgreSQL upgrade check failed during {args[0]}: {diagnostic}"
            )
        return result.stdout.strip()

    def sql(container, statement):
        return docker(
            "exec",
            "-i",
            container,
            "psql",
            "-XAt",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "postgres",
            stdin=statement,
        )

    def start(image, volume, suffix):
        name = label + "-" + suffix
        docker(
            "run",
            "-d",
            "--name",
            name,
            "--label",
            "devops.test=" + label,
            "--network",
            "none",
            "--memory",
            "512m",
            "--cpus",
            "1",
            "--pids-limit",
            "128",
            "--env",
            "POSTGRES_PASSWORD",
            "--mount",
            f"type=volume,source={volume},target=/var/lib/postgresql/data",
            image,
        )
        containers.append(name)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            ready = subprocess.run(
                ["docker", "exec", name, "pg_isready", "-U", "postgres"],
                capture_output=True,
            )
            if ready.returncode == 0:
                # initdb briefly runs a temporary server; wait for the final server.
                state = json.loads(docker("inspect", name))[0]
                if state["State"]["Running"]:
                    try:
                        if (
                            sql(name, "SELECT pg_postmaster_start_time() IS NOT NULL;")
                            == "t"
                        ):
                            time.sleep(2)
                            return name
                    except RuntimeError:
                        pass
            time.sleep(1)
        raise RuntimeError("Test PostgreSQL did not become ready")

    try:
        old_id = json.loads(docker("image", "inspect", old_image))[0]["Id"]
        new_id = json.loads(docker("image", "inspect", new_image))[0]["Id"]
        for suffix in ("original",):
            name = label + "-" + suffix
            docker(
                "volume",
                "create",
                "--label",
                "devops.test=" + label,
                "--label",
                "com.docker.compose.project=" + label,
                "--label",
                "com.docker.compose.volume=pgdata",
                name,
            )
            volumes.append(name)
        original = start(old_id, volumes[0], "old")
        old_version = sql(original, "SHOW server_version;")
        sql(
            original,
            'CREATE TABLE upgrade_probe (id int PRIMARY KEY, payload jsonb NOT NULL); INSERT INTO upgrade_probe VALUES (1, \'{"text":"данные","n":42}\');',
        )
        expected = sql(
            original,
            "SELECT md5(string_agg(payload::text, ',' ORDER BY id)) FROM upgrade_probe;",
        )
        docker("stop", "--time", "30", original)
        try:
            snapshot = create_snapshot(label, "db", volumes[0], helper_image)
            volumes.append(snapshot["volume"])
            restored = restore_snapshot_to_new_volume(snapshot)
            volumes.append(restored)
        except SnapshotError as error:
            if error.volume and error.volume not in volumes:
                volumes.append(error.volume)
            raise
        candidate = start(new_id, restored, "new")
        new_version = sql(candidate, "SHOW server_version;")
        if old_version.split(".")[0] != new_version.split(".")[0]:
            raise RuntimeError(
                "This check only supports minor-version PostgreSQL upgrades"
            )
        assert (
            sql(
                candidate,
                "SELECT md5(string_agg(payload::text, ',' ORDER BY id)) FROM upgrade_probe;",
            )
            == expected
        )
        sql(candidate, "INSERT INTO upgrade_probe VALUES (2, '{\"new\":true}');")
        assert sql(candidate, "SELECT count(*) FROM upgrade_probe;") == "2"
        docker("stop", "--time", "30", candidate)
        docker("start", original)
        for _ in range(30):
            try:
                assert sql(original, "SELECT count(*) FROM upgrade_probe;") == "1"
                break
            except RuntimeError:
                time.sleep(1)
        else:
            raise RuntimeError("Original cluster did not recover")
        print(
            json.dumps(
                {
                    "old_version": old_version,
                    "new_version": new_version,
                    "data_preserved": True,
                    "new_writes": True,
                    "original_recovered": True,
                }
            )
        )
    finally:
        # Names are generated here, never accepted from user paths or production state.
        for name in reversed(containers):
            docker("rm", "-f", "-v", name)
        for name in reversed(volumes):
            docker("volume", "rm", name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-image", required=True)
    parser.add_argument("--new-image", required=True)
    parser.add_argument(
        "--helper-image",
        required=True,
        help="Trusted runtime image with Python for verified snapshots",
    )
    args = parser.parse_args()
    check_upgrade(args.old_image, args.new_image, args.helper_image)
