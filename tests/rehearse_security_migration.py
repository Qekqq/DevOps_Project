"""Destructive only to a generated test project: migration failure/recovery/success.

GitHub provenance and registry scanning are substituted with explicitly local,
previously scanned images. All service stops, backups, clones, Vault operations,
SQL provisioning, application checks and manual recovery use actual Docker.
Never accepts a project name, production data, credentials or volume names.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import hvac
import yaml

from scripts import deploy_release as deploy
from scripts import security_migration as migration
from scripts import start_stack
from scripts.infrastructure_release import apply_images
from scripts.model_delivery import inventory

ROOT = Path(__file__).resolve().parents[1]


def main():
    project = "devops_migration_" + uuid4().hex[:10]
    home = ROOT / ".local-history" / "migration-rehearsal" / project
    home.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ, COMPOSE_DISABLE_ENV_FILE="1")
    feedback = home / "feedback"
    feedback.mkdir()
    api = "devops-security-api:check"
    frontend = "devops-security-frontend:check"
    override = {"services": {}}
    override["services"]["grafana"] = {
        "image": "devops-security-grafana:check",
        "pull_policy": "never",
    }
    for name in ("diabetes-api", "kafka-consumer", "metrics-exporter", "docker-stats"):
        override["services"][name] = {"image": api, "pull_policy": "never"}
    override["services"]["diabetes-api"]["volumes"] = [f"{feedback}:/app/data/feedback"]
    override["services"]["frontend"] = {
        "image": frontend,
        "pull_policy": "never",
        "ports": ["127.0.0.1:18080:8080"],
    }
    override["services"]["db"] = {"ports": ["127.0.0.1:15433:5432"]}
    override["services"]["vault"] = {"ports": ["127.0.0.1:18201:8200"]}
    for name in ("alloy", "docker-stats"):
        override["services"].setdefault(name, {})["environment"] = {
            "COMPOSE_PROJECT_NAME": project
        }
    override_file = home / "override.yml"
    # !override prevents retaining the production loopback port in a merged list.
    content = yaml.safe_dump(override)
    content = content.replace("ports:\n", "ports: !override\n")
    override_file.write_text(content, encoding="utf-8")
    compose = [
        "docker",
        "compose",
        "-p",
        project,
        "-f",
        str(ROOT / "docker-compose.yml"),
        "-f",
        str(override_file),
    ]
    bootstrap = {}
    real_initialize = hvac.api.system_backend.SystemBackend.initialize
    real_read = deploy.read_release
    real_command = migration.command
    real_maintenance = migration.run_maintenance
    real_open = deploy.urllib.request.urlopen
    real_check = deploy.check_application

    def initialize(self, *args, **kwargs):
        result = real_initialize(self, *args, **kwargs)
        bootstrap.update(
            root_token=result["root_token"], unseal_key=result["keys_base64"][0]
        )
        return result

    def local_release(folder, expected_commit=None, expected_run=None):
        # Fixtures use immutable local config IDs, never publish to a registry.
        folder = Path(folder)
        manifest = json.loads((folder / "release.json").read_text())
        assert not expected_commit or manifest["commit"] == expected_commit
        assert not expected_run or manifest["ci_run_id"] == expected_run
        for name, digest in manifest["files"].items():
            assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == digest
        config = json.loads((folder / "docker-compose.json").read_text())
        assert all(
            s["image"].startswith("sha256:") and "build" not in s
            for s in config["services"].values()
        )
        return manifest, config

    def command(*args, **kwargs):
        if args[0] == "pull":
            assert args[1].startswith("sha256:")
            return ""  # Already available, immutable and separately scanned.
        try:
            return real_command(*args, **kwargs)
        except RuntimeError as error:
            details = getattr(error, "_private_diagnostic", "")
            for line in details.splitlines():
                if "error" in line.lower() and not re.search(
                    r"password|secret|token|authorization|cookie", line, re.I
                ):
                    print("Synthetic Compose error:", line[:1500], flush=True)
            print(
                real_command(
                    "ps",
                    "-a",
                    "--filter",
                    "label=com.docker.compose.project=" + project,
                    "--format",
                    "{{.Names}} {{.Status}}",
                ),
                flush=True,
            )
            for name in (
                "diabetes-api",
                "kafka-consumer",
                "metrics-exporter",
                "alloy",
                "docker-stats",
                "frontend",
            ):
                container = project + "-" + name + "-1"
                info = json.loads(real_command("inspect", container))[0]
                if info["State"].get("Error"):
                    print(
                        "Synthetic startup error:",
                        name,
                        info["State"]["Error"],
                        flush=True,
                    )
                if (
                    info["State"].get("Health", {}).get("Status") == "unhealthy"
                    or not info["State"]["Running"]
                ):
                    result = subprocess.run(
                        ["docker", "logs", "--tail", "12", container],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                    )
                    print(
                        "Synthetic service diagnostic:",
                        name,
                        result.stdout,
                        result.stderr,
                        flush=True,
                    )
            raise

    def local_open(url, *args, **kwargs):
        if isinstance(url, str):
            url = url.replace("http://127.0.0.1:8080/", "http://127.0.0.1:18080/")
        return real_open(url, *args, **kwargs)

    def check():
        with patch.object(deploy.urllib.request, "urlopen", local_open):
            real_check()

    def image_id(name):
        return json.loads(real_command("image", "inspect", name))[0]["Id"]

    def package(path, config, sha, generation=None):
        path.mkdir(parents=True, exist_ok=False)
        for directory in ("monitoring", "vault", "db"):
            shutil.copytree(ROOT / directory, path / directory)
        (path / "docker-compose.json").write_text(json.dumps(config), encoding="utf-8")
        files = {
            p.relative_to(path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in path.rglob("*")
            if p.is_file()
        }
        manifest = {
            "commit": sha,
            "ci_run_id": "123",
            "files": files,
            "model_files": inventory(ROOT / "models"),
        }
        if generation:
            manifest["infrastructure_generation"] = generation
        (path / "release.json").write_text(json.dumps(manifest), encoding="utf-8")

    def sql(text):
        return real_command(
            "exec",
            "-i",
            f"{project}-db-1",
            "psql",
            "-U",
            "diabetes",
            "-d",
            "diabetes",
            "-At",
            "-v",
            "ON_ERROR_STOP=1",
            payload=text,
        )

    try:
        with (
            patch.object(
                hvac.api.system_backend.SystemBackend, "initialize", initialize
            ),
            patch.object(
                sys,
                "argv",
                [
                    "start_stack",
                    "--ci",
                    "--no-build",
                    "--project",
                    project,
                    "--compose-file",
                    str(override_file),
                    "--vault-url",
                    "http://127.0.0.1:18201",
                    "--api-url",
                    "http://127.0.0.1:18080/api",
                ],
            ),
        ):
            start_stack.main()
        config = json.loads(
            subprocess.check_output(
                compose + ["config", "--no-interpolate", "--format", "json"],
                env=env,
                encoding="utf-8",
            )
        )
        for service in config["services"].values():
            service.pop("build", None)
            service["image"] = image_id(service["image"])
        old = home / "releases" / "legacy"
        package(old, config, "a" * 40)
        (old / "runtime.json").write_text(json.dumps(config), encoding="utf-8")
        migration.atomic_json(
            home / "current.json",
            {"commit": "a" * 40, "ci_run_id": "123", "release": str(old)},
        )
        candidate = json.loads(json.dumps(config))
        apply_images(
            candidate,
            {
                "db": image_id("devops-security-postgres:check"),
                "vault": image_id("devops-security-vault:check"),
                "kafka": image_id("devops-security-kafka:check"),
            },
        )
        release = home / "candidate"
        package(release, candidate, "b" * 40, generation=2)
        sql(
            "CREATE TABLE security_migration_probe (id integer primary key, value text); INSERT INTO security_migration_probe VALUES (1,'synthetic-original');"
        )
        failed = False

        def maintenance(*args, **kwargs):
            nonlocal failed
            if args[3] == ["provision-runtime"] and not failed:
                failed = True
                raise RuntimeError("Intentional rehearsal interruption")
            return real_maintenance(*args, **kwargs)

        with (
            patch.object(deploy, "PROJECT", project),
            patch.object(deploy, "read_release", local_release),
            patch.object(deploy, "check_application", check),
            patch.object(migration, "verify_source", lambda *a: {"test_only": True}),
            patch.object(migration, "scan", lambda *a: True),
            patch.object(migration, "command", command),
        ):
            with deploy.deployment_lock(home):
                with patch.object(migration, "run_maintenance", maintenance):
                    try:
                        migration.migrate(release, bootstrap, home)
                    except RuntimeError as error:
                        if str(error) != "Intentional rehearsal interruption":
                            if isinstance(
                                error.__context__,
                                migration.requests.exceptions.SSLError,
                            ):
                                print(
                                    "Synthetic TLS failure:",
                                    str(error.__context__),
                                    flush=True,
                                )
                            raise
                    else:
                        raise AssertionError("Failure injection was not reached")
            assert failed and (home / migration.PENDING).exists()
            assert json.loads((home / "current.json").read_text())["commit"] == "a" * 40
            with deploy.deployment_lock(home, allow_pending_migration=True):
                migration.restore(home, bootstrap)
            assert (
                sql("SELECT value FROM security_migration_probe WHERE id=1;")
                == "synthetic-original"
            )
            assert not (home / migration.PENDING).exists()
            with deploy.deployment_lock(home):
                migration.migrate(release, bootstrap, home)
            assert (
                sql("SELECT value FROM security_migration_probe WHERE id=1;")
                == "synthetic-original"
            )
            assert json.loads((home / "current.json").read_text())["commit"] == "b" * 40
            assert not (home / migration.PENDING).exists()
            services = deploy.existing_services()
            for name in migration.SERVICES:
                settings = migration.environment(services[name])
                assert settings["VAULT_ADDR"] == "https://vault:8200"
                assert (
                    settings["VAULT_ROLE_ID_FILE"] and settings["VAULT_SECRET_ID_FILE"]
                )
                assert not settings.get("VAULT_ROLE_ID") and not settings.get(
                    "VAULT_SECRET_ID"
                )
            target = json.loads((home / "current.json").read_text())
            runtime = json.loads((Path(target["release"]) / "runtime.json").read_text())
            assert all(
                runtime["volumes"][n]["external"]
                for n in migration.SERVICE_VOLUMES.values()
            )
            print(
                "Migration rehearsal: intentional failure, manual recovery, second successful migration, data and file identities verified.",
                flush=True,
            )
    finally:
        deploy.read_release = real_read
        ids = real_command(
            "ps", "-aq", "--filter", "label=com.docker.compose.project=" + project
        ).splitlines()
        if ids:
            real_command("rm", "-f", "-v", *ids)
        volumes = set()
        for label in (
            "com.docker.compose.project=" + project,
            "devops.snapshot.project=" + project,
        ):
            volumes.update(
                real_command(
                    "volume", "ls", "-q", "--filter", "label=" + label
                ).splitlines()
            )
        for volume in sorted(volumes):
            assert volume.startswith(project + "_")
            real_command("volume", "rm", volume)
        for network in real_command(
            "network",
            "ls",
            "-q",
            "--filter",
            "label=com.docker.compose.project=" + project,
        ).splitlines():
            real_command("network", "rm", network)


if __name__ == "__main__":
    main()
