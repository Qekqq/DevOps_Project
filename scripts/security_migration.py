"""One-time, journalled migration of the reviewed legacy Windows installation.

No local builds, automatic rollback, volume deletion, or public deployment.
"""

import argparse
import base64
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import hvac
import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from scripts import deploy_release as deploy
from scripts.backup_database import create_database_backup
from scripts.infrastructure_release import KAFKA_ENV
from scripts.maintenance_client import installed_admin_credentials, run_maintenance
from scripts.model_delivery import stage_models
from scripts.release_provenance import verify_source
from scripts.scan_images import scan
from scripts.security_migration_plan import DATA_PATHS, build_plan
from scripts.start_stack import (
    KEEPASS_PATH_FILE,
    KeePassCredentials,
    configure_vault,
    wait_until,
)
from scripts.vault_connection import environment, host_bind_source, installed_connection
from scripts.vault_identity import SERVICES, write_identities
from scripts.vault_tls import prepare_tls
from scripts.volume_snapshot import (
    SERVICE_VOLUMES,
    create_snapshot,
    restore_snapshot_to_new_volume,
)

LEGACY = {
    "db": "sha256:081f1bc7bd5e143dbb6e487b710bbc27712cdcfaced4c071b8e47349aa1b4171",
    "vault": "sha256:74a4ab138ab5d64725e89cd9a9c73f7040c7fe49e98b71697b275ca9a69919df",
    "kafka": "sha256:c274f870840a5d854fb601408c398e596c795582d869b5d85c7f7d882e5c478d",
}
PENDING = "security-migration.pending.json"


def command(*args, payload=None, env=None, timeout=240):
    result = subprocess.run(
        ["docker", *args],
        input=payload,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )
    if result.returncode:
        error = RuntimeError(
            f"Migration Docker {args[0]} failed; private diagnostics suppressed"
        )
        # Never part of str(error) or CLI output. The synthetic rehearsal may
        # inspect this field while no real credentials/data are in its services.
        error._private_diagnostic = result.stderr
        raise error
    return result.stdout.strip()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def recovery_key(unseal_key, salt):
    if not isinstance(unseal_key, str) or len(unseal_key) < 32:
        raise ValueError("Invalid Vault recovery key")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=b"devops/security-migration/recovery/v1",
    ).derive(unseal_key.encode())


def seal_recovery(value, unseal_key, migration_id):
    salt, nonce = os.urandom(16), os.urandom(12)
    aad = (deploy.PROJECT + "/" + migration_id).encode()
    encrypted = AESGCM(recovery_key(unseal_key, salt)).encrypt(
        nonce,
        json.dumps(value).encode(),
        aad,
    )
    return {
        "format": 1,
        "migration_id": migration_id,
        "payload": base64.b64encode(salt + nonce + encrypted).decode("ascii"),
    }


def open_recovery(value, unseal_key, migration_id):
    if value.get("format") != 1 or value.get("migration_id") != migration_id:
        raise ValueError("Recovery bundle identity differs")
    content = base64.b64decode(value["payload"], validate=True)
    aad = (deploy.PROJECT + "/" + migration_id).encode()
    return json.loads(
        AESGCM(recovery_key(unseal_key, content[:16])).decrypt(
            content[16:28],
            content[28:],
            aad,
        )
    )


def get_bootstrap(database):
    database = database or os.getenv("KEEPASS_DB")
    if not database and KEEPASS_PATH_FILE.is_file():
        database = KEEPASS_PATH_FILE.read_text(encoding="utf-8").strip()
    if not database:
        raise RuntimeError(
            "Provide KEEPASS_DB pointing to the existing KeePass database"
        )
    return KeePassCredentials(
        database, deploy.PROJECT, os.getenv("KEEPASS_KEY_FILE")
    ).read()


def client_for(url, bootstrap, ca=True):
    session = requests.Session()
    session.trust_env = False
    # HVAC gives an explicitly supplied session's verify setting precedence.
    session.verify = ca
    client = hvac.Client(
        url=url,
        token=bootstrap["root_token"],
        verify=ca,
        timeout=10,
        session=session,
        allow_redirects=False,
    )
    return client


def unseal(client, bootstrap):
    def ready():
        try:
            return client.sys.read_seal_status() is not None
        except requests.exceptions.SSLError as error:
            # Replacing an HTTP listener with HTTPS can briefly reset handshakes
            # in Docker's port forwarding. Retry those, never a bad certificate.
            if "CERTIFICATE_VERIFY_FAILED" in str(error):
                raise RuntimeError(
                    "Vault TLS verification failed; trust was not changed"
                ) from None
            raise

    wait_until(ready, "Vault is unavailable")
    if not client.sys.is_initialized():
        raise RuntimeError(
            "Copied Vault is unexpectedly empty; initialization is forbidden"
        )
    if client.sys.is_sealed():
        client.sys.submit_unseal_key(bootstrap["unseal_key"])
    if client.sys.is_sealed() or not client.is_authenticated():
        raise RuntimeError("Vault recovery credentials did not work")


def validate_legacy(services, manifest):
    if manifest.get("infrastructure_generation") != 2:
        raise ValueError("A generation 2 CI package is required")
    for name, expected in LEGACY.items():
        image = deploy.docker_json("image", "inspect", services[name]["Image"])[0]
        digests = {item.rsplit("@", 1)[-1] for item in image.get("RepoDigests", [])}
        if expected not in digests:
            raise RuntimeError(
                f"Unsupported installed {name} image; a new migration review is required"
            )
    for name, container in services.items():
        if not container["State"]["Running"] or container["State"].get(
            "Health", {}
        ).get("Status") in {"starting", "unhealthy"}:
            raise RuntimeError(f"Installed {name} is not ready")
    for name in SERVICES:
        env = environment(services[name])
        if env.get("VAULT_ADDR") != "http://vault:8200" or env.get(
            "VAULT_ROLE_ID_FILE"
        ):
            raise RuntimeError(
                "This one-time migration requires the reviewed legacy Vault setup"
            )


def mounted_config(config, services):
    """Capture exact installed image IDs and mounts for encrypted manual recovery."""
    config = copy.deepcopy(config)
    config["name"] = deploy.PROJECT
    for name, service in config["services"].items():
        if name not in services:
            raise RuntimeError("Installed runtime and containers disagree")
        container = services[name]
        service.pop("build", None)
        service["image"] = container["Image"]
        service["environment"] = environment(container)
        if container.get("HostConfig", {}).get("GroupAdd"):
            service["group_add"] = container["HostConfig"]["GroupAdd"]
        service["command"] = container["Config"].get("Cmd") or []
        service["entrypoint"] = container["Config"].get("Entrypoint") or []
        if container["Config"].get("Healthcheck"):
            service.setdefault("healthcheck", {})["test"] = container["Config"][
                "Healthcheck"
            ]["Test"]
        mounts = []
        for index, mount in enumerate(container["Mounts"]):
            if mount["Type"] == "volume":
                logical = f"recovery_{name}_{index}".replace("-", "_")
                config.setdefault("volumes", {})[logical] = {
                    "name": mount["Name"],
                    "external": True,
                }
                source = logical
            elif mount["Type"] == "bind":
                if mount["Destination"] == "/var/run/docker.sock":
                    declared = [
                        m
                        for m in service.get("volumes", [])
                        if m.get("target") == "/var/run/docker.sock"
                    ]
                    if (
                        len(declared) != 1
                        or declared[0].get("source") != "/var/run/docker.sock"
                    ):
                        raise RuntimeError(
                            "Unsupported Docker socket mapping for recovery"
                        )
                    # Docker Desktop inspect reports an internal proxy socket.
                    # Compose on Windows must receive the original portable path.
                    source = "/var/run/docker.sock"
                else:
                    source = host_bind_source(mount["Source"])
            elif mount["Type"] == "tmpfs":
                continue
            else:
                raise RuntimeError("Unsupported installed mount for manual recovery")
            mounts.append(
                {
                    "type": mount["Type"],
                    "source": source,
                    "target": mount["Destination"],
                    "read_only": not mount["RW"],
                }
            )
        service["volumes"] = mounts
    return config


def connect_data(config, volumes):
    for name, source in volumes.items():
        logical = SERVICE_VOLUMES[name]
        config.setdefault("volumes", {})[logical] = {"name": source, "external": True}
        mounts = config["services"][name]["volumes"]
        matched = [m for m in mounts if m["target"] == DATA_PATHS[name]]
        if len(matched) != 1:
            raise RuntimeError("Candidate data mount differs from reviewed layout")
        matched[0].update(type="volume", source=logical, read_only=False)


def configure_tls(config, folder, ca_file):
    config.setdefault("volumes", {})["vault_tls"] = {
        "name": deploy.PROJECT + "_vault_tls",
        "external": True,
    }
    vault = config["services"]["vault"]
    mounts = vault["volumes"]
    mounts[:] = [
        m
        for m in mounts
        if m["target"] not in {"/vault/config/server.hcl", "/vault/tls"}
    ]
    mounts += [
        {
            "type": "bind",
            "source": str(folder / "vault/server-tls.hcl"),
            "target": "/vault/config/server.hcl",
            "read_only": True,
        },
        {
            "type": "volume",
            "source": "vault_tls",
            "target": "/vault/tls",
            "read_only": True,
        },
    ]
    vault["healthcheck"]["test"] = [
        "CMD-SHELL",
        "VAULT_ADDR=https://127.0.0.1:8200 VAULT_CACERT=/vault/tls/ca.crt vault status >/dev/null 2>&1",
    ]
    for name in SERVICES:
        service = config["services"][name]
        service["environment"].update(
            VAULT_ADDR="https://vault:8200", VAULT_CACERT="/run/vault/ca.crt"
        )
        service.setdefault("volumes", []).append(
            {
                "type": "bind",
                "source": str(ca_file),
                "target": "/run/vault/ca.crt",
                "read_only": True,
            }
        )


def check_space(plan, helper, home):
    mounts = []
    for item in plan["infrastructure"]:
        mounts += [
            "--mount",
            f"type=volume,source={item['data_volume']},target=/data/{item['service']},readonly",
        ]
    program = """
import json, shutil
from pathlib import Path
roots=[Path('/data') / n for n in ('db','vault','kafka')]
size=0
for root in roots:
    for p in root.rglob('*'):
        if p.is_symlink() or not (p.is_file() or p.is_dir()):
            raise RuntimeError('Unsupported data entry')
        size += (p.stat().st_size if p.is_file() else 0) + 8192
required=size*3+1024**3
if min(shutil.disk_usage(root).free for root in roots) < required:
    raise RuntimeError('Insufficient Docker storage for migration')
print(json.dumps({'estimated_bytes':size,'required_free_bytes':required}))
"""
    output = command(
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--user",
        "0:0",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "DAC_READ_SEARCH",
        "--security-opt",
        "no-new-privileges",
        *mounts,
        "--entrypoint",
        "python",
        helper,
        "-c",
        program,
    )
    estimate = json.loads(output)
    # Docker Desktop's disk location is not necessarily the project drive.
    # Check every fixed Windows disk (conservative refusal, never assume C:).
    if os.name == "nt":
        import ctypes

        roots = [
            Path(f"{letter}:/")
            for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            if ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\") == 3
        ]
    else:
        roots = [home]
    if any(
        shutil.disk_usage(root).free < estimate["required_free_bytes"] for root in roots
    ):
        raise RuntimeError(
            "Insufficient host disk space for snapshots and database backup"
        )
    return estimate


def run_config(config, *args):
    # Resolved recovery environment may contain secrets: stdin only, no host file.
    # Prevent Compose from expanding literal dollars in passwords or commands.
    def escape(value):
        if isinstance(value, str):
            return value.replace("$", "$$")
        if isinstance(value, list):
            return [escape(v) for v in value]
        if isinstance(value, dict):
            return {k: escape(v) for k, v in value.items()}
        return value

    return command(
        "compose",
        "-p",
        deploy.PROJECT,
        "-f",
        "-",
        *args,
        payload=json.dumps(escape(config)),
        env={**os.environ, "COMPOSE_DISABLE_ENV_FILE": "1"},
    )


def migrate(folder, bootstrap, home):
    manifest, candidate = deploy.read_release(folder)
    provenance = verify_source(folder, manifest)
    services = deploy.existing_services()
    validate_legacy(services, manifest)
    current = json.loads((home / "current.json").read_text(encoding="utf-8"))
    old_folder = Path(current["release"]).resolve(strict=True)
    if not old_folder.is_relative_to((home / "releases").resolve()):
        raise ValueError("Installed release is outside deployment storage")
    deploy.read_release(old_folder, current["commit"], current["ci_run_id"])
    plan = build_plan(services, current, candidate)
    env = deploy.service_environment(services)
    connection = installed_connection(services)
    old_client = client_for(connection.url.removesuffix("/v1/"), bootstrap)
    if old_client.sys.is_sealed() or not old_client.is_authenticated():
        raise RuntimeError(
            "Existing Vault must be unsealed and KeePass credentials valid"
        )
    if "root" not in old_client.auth.token.lookup_self()["data"]["policies"]:
        raise RuntimeError(
            "The reviewed migration requires the stored Vault bootstrap identity"
        )
    old_client.session.close()
    original_config = json.loads(
        command(
            "compose",
            "-p",
            deploy.PROJECT,
            "-f",
            str(old_folder / "runtime.json"),
            "config",
            "--format",
            "json",
            env=env,
        )
    )
    recovery = mounted_config(original_config, services)
    run_config(recovery, "config", "--quiet")
    migration_id = uuid4().hex
    directory = home / "security-migrations" / migration_id
    directory.mkdir(parents=True, exist_ok=False)
    state = {
        "format": 1,
        "migration_id": migration_id,
        "project": deploy.PROJECT,
        "provenance": provenance,
        "plan": plan,
        "stage": "preflight",
        "snapshots": {},
        "new_volumes": {},
    }

    def record(stage):
        state["stage"] = stage
        atomic_json(directory / "journal.json", state)
        print(f"Перенос: {stage}", flush=True)

    images = list(dict.fromkeys(s["image"] for s in candidate["services"].values()))
    for index, image in enumerate(images):
        command("pull", image, timeout=600)
        if not scan(image, directory / "scans" / str(index)):
            raise RuntimeError("Candidate image security scan blocks migration")
    helper = candidate["services"]["diabetes-api"]["image"]
    state["storage"] = check_space(plan, helper, home)
    properties = command(
        "exec", services["kafka"]["Id"], "cat", "/bitnami/kafka/data/meta.properties"
    )
    cluster_id = dict(
        line.split("=", 1) for line in properties.splitlines() if "=" in line
    ).get("cluster.id", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{22}", cluster_id):
        raise RuntimeError("Cannot verify the existing Kafka cluster ID")
    if (
        command(
            "exec", services["db"]["Id"], "cat", "/var/lib/postgresql/data/PG_VERSION"
        )
        != "16"
    ):
        raise RuntimeError("Only PostgreSQL major version 16 was rehearsed")
    package_id = hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode()
    ).hexdigest()[:12]
    destination = (
        home
        / "releases"
        / f"{manifest['commit']}-{package_id}-security-{migration_id[:12]}"
    )
    if destination.exists():
        raise RuntimeError(
            "Migration destination already exists; inspect the previous attempt"
        )
    shutil.copytree(folder, destination)
    stage_models(manifest, deploy.ROOT / "models", destination / "models")
    runtime = deploy.configure_runtime(candidate, destination, services)
    runtime["services"]["kafka"]["environment"] = dict(KAFKA_ENV, CLUSTER_ID=cluster_id)
    # Recheck before the first service stop, after potentially lengthy downloads.
    if (
        build_plan(deploy.existing_services(), current, candidate)["fingerprint"]
        != plan["fingerprint"]
    ):
        raise RuntimeError("Installed containers changed during preflight")
    bundle = seal_recovery(
        {
            "config": recovery,
            "current": current,
            "vault_url": connection.url,
            "plan": plan,
        },
        bootstrap["unseal_key"],
        migration_id,
    )
    atomic_json(directory / "recovery.enc.json", bundle)
    if (
        open_recovery(bundle, bootstrap["unseal_key"], migration_id)["config"]
        != recovery
    ):
        raise RuntimeError("Recovery bundle verification failed")
    # Publish recovery and journal before the marker that blocks ordinary starts.
    # A crash immediately after the marker must still leave a complete runbook.
    atomic_json(directory / "journal.json", state)
    atomic_json(home / PENDING, {"migration_id": migration_id})
    try:
        record("stop-writers")
        command(
            "stop",
            "--time",
            "60",
            *(c["Id"] for n, c in services.items() if n not in DATA_PATHS),
        )
        state["database_backup"] = str(
            create_database_backup(home / "backups", container=services["db"]["Id"])
        )
        record("stop-infrastructure")
        command("stop", "--time", "60", *(services[n]["Id"] for n in DATA_PATHS))
        for item in plan["infrastructure"]:
            name = item["service"]
            state["snapshots"][name] = create_snapshot(
                deploy.PROJECT, name, item["data_volume"], helper
            )
            record("snapshot-" + name)
        for name, snapshot in state["snapshots"].items():
            state["new_volumes"][name] = restore_snapshot_to_new_volume(snapshot)
            record("restore-" + name)
        # Only the newly restored Kafka volume changes UID (Bitnami -> Apache).
        command(
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
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
            "--security-opt",
            "no-new-privileges",
            "--mount",
            f"type=volume,source={state['new_volumes']['kafka']},target=/data",
            "--entrypoint",
            "python",
            helper,
            "-c",
            "import os; from pathlib import Path; r=Path('/data'); "
            "[os.chown(p,1000,1000,follow_symlinks=False) for p in [r,*r.rglob('*')]]",
        )
        connect_data(runtime, state["new_volumes"])
        ca = prepare_tls(deploy.PROJECT, helper, home / "vault-tls")
        configure_tls(runtime, destination, ca)
        runtime_path = destination / "runtime.json"
        atomic_json(runtime_path, runtime)
        prefix = ["compose", "-p", deploy.PROJECT, "-f", str(runtime_path)]

        def up(*names):
            command(
                *prefix,
                "up",
                "-d",
                "--no-deps",
                "--no-build",
                "--pull",
                "never",
                *names,
                env=env,
            )

        record("start-new-vault")
        up("vault")
        target_client = client_for(
            connection.url.removesuffix("/v1/").replace("http://", "https://", 1),
            bootstrap,
            str(ca),
        )
        unseal(target_client, bootstrap)
        database, identities = configure_vault(
            target_client,
            installed_admin_credentials(services),
            restricted_database=True,
        )
        overlay = write_identities(deploy.PROJECT, helper, identities, replace=True)
        runtime["volumes"].update(
            {k: {**v, "external": True} for k, v in overlay["volumes"].items()}
        )
        for name, settings in overlay["services"].items():
            runtime["services"][name]["environment"].update(settings["environment"])
            runtime["services"][name]["volumes"].extend(settings["volumes"])
        for name, service_path in (
            ("diabetes-api", "api"),
            ("kafka-consumer", "consumer"),
            ("metrics-exporter", "metrics"),
        ):
            runtime["services"][name]["environment"]["VAULT_DB_SECRET_PATH"] = (
                "database/" + service_path
            )
        for key in identities:
            env.pop(key, None)
        atomic_json(runtime_path, runtime)
        record("start-new-database-and-kafka")
        command(
            *prefix,
            "up",
            "-d",
            "--no-deps",
            "--no-build",
            "--pull",
            "never",
            "--wait",
            "--wait-timeout",
            "180",
            "db",
            "kafka",
            env=env,
        )
        maintenance_prefix = ["docker", *prefix]
        run_maintenance(maintenance_prefix, env, database, ["scripts.update_database"])
        metrics = target_client.secrets.kv.v2.read_secret_version(
            path="database/metrics", raise_on_deleted_version=True
        )["data"]["data"]
        passwords = {
            "diabetes_" + name: target_client.secrets.kv.v2.read_secret_version(
                path="database/" + name, raise_on_deleted_version=True
            )["data"]["data"]["POSTGRES_PASSWORD"]
            for name in ("api", "consumer")
        }
        run_maintenance(
            maintenance_prefix,
            env,
            database,
            ["provision-runtime"],
            extra={"identities": passwords, "metrics": metrics},
        )
        target_client.session.close()
        record("new-infrastructure-ready")
        state["runtime"] = str(runtime_path)
        state["release"] = str(destination)
        deploy.prepare_application_storage(runtime_path, env, services)
        for task in (
            ["src.register_release", "models/current.json", "--apply"],
            [
                "scripts.activate_model_release",
                "models/current.json",
                "--if-no-champion",
            ],
        ):
            run_maintenance(maintenance_prefix, env, database, task)
        record("start-application")
        names = [
            *deploy.APPLICATION,
            *deploy.MONITORING,
            *(n for n in deploy.OPTIONAL_MONITORING if n in runtime["services"]),
        ]
        command(
            *prefix,
            "up",
            "-d",
            "--no-deps",
            "--no-build",
            "--pull",
            "never",
            "--wait",
            "--wait-timeout",
            "180",
            *names,
            env=env,
        )
        deploy.check_application()
        record("application-verified")
        atomic_json(
            home / "current.json",
            {
                "commit": manifest["commit"],
                "ci_run_id": manifest["ci_run_id"],
                "release": str(destination),
                "database_backup": state["database_backup"],
                "security_migration": migration_id,
            },
        )
        record("complete")
        (home / PENDING).unlink()
        print(
            "Перенос завершён. Исходные тома и снимки сохранены для ручного восстановления."
        )
    except BaseException:
        state["failed_at"] = state["stage"]
        record("failed")
        raise


def restore(home, bootstrap):
    """Manual recovery of a pending attempt. Attempted new volumes are retained."""
    pending = json.loads((home / PENDING).read_text(encoding="utf-8"))
    migration_id = pending["migration_id"]
    if not re.fullmatch(r"[0-9a-f]{32}", migration_id):
        raise ValueError("Invalid pending migration identifier")
    directory = home / "security-migrations" / migration_id
    bundle = json.loads((directory / "recovery.enc.json").read_text(encoding="utf-8"))
    recovery = open_recovery(bundle, bootstrap["unseal_key"], migration_id)
    config = recovery["config"]
    if config["name"] != deploy.PROJECT:
        raise ValueError("Recovery configuration belongs to another project")
    for record in recovery["plan"]["infrastructure"]:
        volume = deploy.docker_json("volume", "inspect", record["data_volume"])[0]
        labels = volume.get("Labels") or {}
        if (
            labels.get("com.docker.compose.project") != deploy.PROJECT
            or labels.get("com.docker.compose.volume")
            != SERVICE_VOLUMES[record["service"]]
        ):
            raise RuntimeError("Original recovery volume ownership differs")
    for service in config["services"].values():
        if "build" in service or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", service["image"]
        ):
            raise RuntimeError("Recovery requires retained original image IDs")
        deploy.docker_json("image", "inspect", service["image"])
    run_config(config, "config", "--quiet")
    ids = command(
        "ps", "-aq", "--filter", "label=com.docker.compose.project=" + deploy.PROJECT
    ).splitlines()
    if ids:
        command("stop", "--time", "60", *ids)
    run_config(
        config, "up", "-d", "--no-deps", "--no-build", "--pull", "never", "vault"
    )
    client = client_for(recovery["vault_url"].removesuffix("/v1/"), bootstrap)
    unseal(client, bootstrap)
    client.session.close()
    run_config(
        config,
        "up",
        "-d",
        "--no-deps",
        "--no-build",
        "--pull",
        "never",
        "--wait",
        "--wait-timeout",
        "180",
        "db",
        "kafka",
    )
    names = [name for name in config["services"] if name not in DATA_PATHS]
    run_config(
        config,
        "up",
        "-d",
        "--no-deps",
        "--no-build",
        "--pull",
        "never",
        "--wait",
        "--wait-timeout",
        "180",
        *names,
    )
    deploy.check_application()
    atomic_json(home / "current.json", recovery["current"])
    state = json.loads((directory / "journal.json").read_text(encoding="utf-8"))
    state["stage"] = "restored-original"
    atomic_json(directory / "journal.json", state)
    (home / PENDING).unlink()
    print(
        "Исходная установка восстановлена. Тома неудачной попытки сохранены отдельно."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path)
    parser.add_argument("--keepass-db")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--restore", action="store_true")
    args = parser.parse_args()
    if not args.apply or bool(args.release) == bool(args.restore):
        parser.error("Use --apply with either --release or --restore")
    home = deploy.deployment_home()
    try:
        with deploy.deployment_lock(home, allow_pending_migration=args.restore):
            if args.restore and not (home / PENDING).is_file():
                raise RuntimeError("Нет незавершённого переноса для восстановления")
            bootstrap = get_bootstrap(args.keepass_db)
            if args.restore:
                restore(home, bootstrap)
            else:
                migrate(args.release, bootstrap, home)
    except (Exception, KeyboardInterrupt) as error:
        detail = (
            str(error)
            if isinstance(error, (RuntimeError, ValueError))
            else type(error).__name__
        )
        recovery = (
            " Незавершённая попытка сохранена. Для ручного возврата: make security-restore."
            if (home / PENDING).exists()
            else " Проверьте сообщение выше и журнал попытки."
        )
        raise SystemExit(f"Перенос не завершён: {detail}.{recovery}") from None


if __name__ == "__main__":
    main()
