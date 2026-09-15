"""First installation from a frozen data export; ordinary CD never calls this."""

import base64
import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from scripts.deploy_release import read_release
from scripts.model_delivery import inventory
from scripts.vault_identity import SERVICES, WRITER
from scripts.vault_tls import issue_identity

ROOT = Path(__file__).resolve().parents[1]

VAULT_REQUEST = """
import json,ssl,sys,urllib.request
p=json.load(sys.stdin)
ctx=ssl.create_default_context(cafile=p['ca'])
request=urllib.request.Request(p['url']+'/v1/'+p['path'],method=p['method'],data=None if p['data'] is None else json.dumps(p['data']).encode(),headers={'Content-Type':'application/json'})
with urllib.request.urlopen(request,context=ctx,timeout=15) as r:
    sys.stdout.buffer.write(r.read())
"""

VAULT_CONFIGURE = """
import json,sys,hvac
from scripts.start_stack import configure_vault
p=json.load(sys.stdin)
c=hvac.Client(url='https://vault:8200',verify='/ca.crt',token=p['root_token'])
database,identities=configure_vault(c,{},restricted_database=True)
metrics=c.secrets.kv.v2.read_secret_version(path='database/metrics',raise_on_deleted_version=True)['data']['data']
passwords={'diabetes_'+n:c.secrets.kv.v2.read_secret_version(path='database/'+n,raise_on_deleted_version=True)['data']['data']['POSTGRES_PASSWORD'] for n in ('api','consumer')}
json.dump({'database':database,'identities':identities,'metrics':metrics,'passwords':passwords},sys.stdout)
"""


def render_runtime(
    config, folder, home, project, origin, *, api_port=8080, vault_port=8201
):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]+", project):
        raise ValueError("Invalid project name")
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/"):
        raise ValueError("Public origin must be an HTTPS origin without a path")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Invalid public origin")
    config = json.loads(json.dumps(config))
    config["name"] = project
    for kind in ("volumes", "networks"):
        for logical, definition in config.get(kind, {}).items():
            if definition.get("external"):
                raise ValueError("A first installation cannot import external storage")
            definition["name"] = project + "_" + logical
    for service in config["services"].values():
        service.pop("ports", None)
        for mount in service.get("volumes", []):
            if mount["type"] == "bind" and mount["source"].startswith("./"):
                relative = PurePosixPath(mount["source"])
                if ".." in relative.parts:
                    raise ValueError("Unsafe release bind path")
                mount["source"] = folder + "/" + str(relative)
    config["services"]["frontend"]["ports"] = [f"127.0.0.1:{api_port}:8080"]
    config["services"]["vault"]["ports"] = [f"127.0.0.1:{vault_port}:8200"]
    config["services"]["db"]["volumes"] = [
        m for m in config["services"]["db"]["volumes"] if m["type"] == "volume"
    ]
    for key in ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"):
        config["services"]["db"]["environment"][key] = (
            "${" + key + ":?Bootstrap required}"
        )
    config["volumes"]["vault_tls"] = {"name": project + "_vault_tls", "external": True}
    vault = config["services"]["vault"]
    for mount in vault["volumes"]:
        if mount["target"] == "/vault/config/server.hcl":
            mount["source"] = folder + "/vault/server-tls.hcl"
    vault["volumes"].append(
        {
            "type": "volume",
            "source": "vault_tls",
            "target": "/vault/tls",
            "read_only": True,
        }
    )
    vault["healthcheck"]["test"] = [
        "CMD-SHELL",
        "VAULT_ADDR=https://127.0.0.1:8200 VAULT_CACERT=/vault/tls/ca.crt vault status >/dev/null 2>&1",
    ]
    for service, prefix in SERVICES.items():
        value = config["services"][service]
        value["environment"].update(
            VAULT_ADDR="https://vault:8200",
            VAULT_CACERT="/run/vault/ca.crt",
            VAULT_ROLE_ID="",
            VAULT_SECRET_ID="",
            VAULT_ROLE_ID_FILE="/run/vault-identity/role_id",
            VAULT_SECRET_ID_FILE="/run/vault-identity/secret_id",
            VAULT_DB_SECRET_PATH="database/"
            + {"API": "api", "CONSUMER": "consumer", "EXPORTER": "metrics"}[prefix],
        )
        identity = prefix.lower() + "_vault_identity"
        config["volumes"][identity] = {
            "name": project + "_" + identity,
            "external": True,
        }
        value["volumes"].extend(
            [
                {
                    "type": "bind",
                    "source": folder + "/vault/ca.crt",
                    "target": "/run/vault/ca.crt",
                    "read_only": True,
                },
                {
                    "type": "volume",
                    "source": identity,
                    "target": "/run/vault-identity",
                    "read_only": True,
                },
            ]
        )
    for mount in config["services"]["diabetes-api"]["volumes"]:
        if mount["target"] == "/app/data/feedback":
            mount["source"] = home + "/data/feedback"
    config["services"]["diabetes-api"]["environment"].update(
        PUBLIC_ORIGIN=origin.rstrip("/"), SESSION_COOKIE_SECURE="true"
    )
    config["services"]["grafana"]["environment"]["GF_SERVER_ROOT_URL"] = (
        origin.rstrip("/") + "/grafana/"
    )
    for service in ("alloy", "docker-stats"):
        config["services"][service]["environment"]["COMPOSE_PROJECT_NAME"] = project
    config["services"]["kafka"]["environment"]["CLUSTER_ID"] = (
        base64.urlsafe_b64encode(os.urandom(16)).decode().rstrip("=")
    )
    return config


def seal_keys(values, key, project):
    nonce = os.urandom(12)
    encrypted = AESGCM(bytes.fromhex(key)).encrypt(
        nonce, json.dumps(values).encode(), project.encode()
    )
    return json.dumps(
        {
            "format": 1,
            "project": project,
            "nonce": nonce.hex(),
            "ciphertext": encrypted.hex(),
        }
    ).encode()


def open_keys(content, key, project):
    data = json.loads(content)
    if data["format"] != 1 or data["project"] != project:
        raise ValueError("Wrong Vault recovery file")
    return json.loads(
        AESGCM(bytes.fromhex(key)).decrypt(
            bytes.fromhex(data["nonce"]),
            bytes.fromhex(data["ciphertext"]),
            project.encode(),
        )
    )


class Bootstrap:
    def __init__(
        self,
        transport,
        home,
        project="devops_project",
        *,
        api_port=8080,
        vault_port=8201,
    ):
        self.remote = transport
        self.home = home.rstrip("/")
        self.project = project
        self.api_port, self.vault_port = api_port, vault_port
        self.env = {"COMPOSE_DISABLE_ENV_FILE": "1", "COMPOSE_PROJECT_NAME": project}

    def compose(self, *args, data=b""):
        return self.remote.docker(
            "compose",
            "-p",
            self.project,
            "-f",
            self.folder + "/runtime.json",
            *args,
            data=data,
            env=self.env,
        )

    def helper(self, program, *, data=None, mounts=(), network="none", root=False):
        args = [
            "run",
            "--rm",
            "-i",
            "--network",
            network,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            "256m",
        ]
        if root:
            args += [
                "--user",
                "0:0",
                "--cap-add",
                "CHOWN",
                "--cap-add",
                "DAC_OVERRIDE",
                "--cap-add",
                "FOWNER",
            ]
        for mount in mounts:
            args += ["--mount", mount]
        return self.remote.docker(
            *args,
            "--entrypoint",
            "python",
            self.image,
            "-c",
            program,
            data=json.dumps(data).encode(),
        )

    def vault(self, path, data=None, method="GET"):
        return json.loads(
            self.remote.python(
                VAULT_REQUEST,
                payload={
                    "ca": self.folder + "/vault/ca.crt",
                    "url": f"https://127.0.0.1:{self.vault_port}",
                    "path": path,
                    "method": method,
                    "data": data,
                },
            )
        )

    def install(
        self,
        release,
        backup,
        feedback,
        recovery_folder,
        store,
        *,
        origin,
        commit,
        run_id,
        expected_counts=None,
    ):
        manifest, config = read_release(release, commit, run_id)
        if inventory(ROOT / "models") != manifest["model_files"]:
            raise ValueError("Models differ from the successful CI release")
        if backup.read_bytes()[:5] != b"PGDMP":
            raise ValueError("Not a PostgreSQL custom-format backup")
        for kind in ("container", "volume", "network"):
            options = ["-a"] if kind == "container" else []
            field = "{{.Names}}" if kind == "container" else "{{.Name}}"
            existing = self.remote.docker(kind, "ls", *options, "--format", field)
            if any(
                name.startswith((self.project + "_", self.project + "-"))
                for name in existing.decode().splitlines()
            ):
                raise RuntimeError(
                    "Target project is not empty; bootstrap refuses replacement"
                )
        self.remote.python(
            "import json,sys; from pathlib import Path; p=Path(json.load(sys.stdin)); "
            "assert not (p/'current.json').exists(), 'Installation already exists'; "
            "p.mkdir(parents=True,exist_ok=True,mode=0o700)",
            payload=self.home,
        )
        # Exclusive state prevents concurrent or repeated initialization.
        self.remote.write(
            self.home + "/bootstrap.pending.json",
            json.dumps({"commit": commit, "run_id": run_id}).encode(),
            mode=0o600,
        )
        package_id = hashlib.sha256(
            json.dumps(manifest, sort_keys=True).encode()
        ).hexdigest()[:12]
        self.folder = self.home + "/releases/" + commit + "-" + package_id
        self.image = config["services"]["diabetes-api"]["image"]
        runtime = render_runtime(
            config,
            self.folder,
            self.home,
            self.project,
            origin,
            api_port=self.api_port,
            vault_port=self.vault_port,
        )
        self.env.update(
            POSTGRES_DB="diabetes",
            POSTGRES_USER="diabetes",
            POSTGRES_PASSWORD=secrets.token_hex(32),
        )
        # Database bootstrap values only enter Compose through private stdin RPC.
        for name in ("release.json", *manifest["files"]):
            self.remote.write(self.folder + "/" + name, (release / name).read_bytes())
        for name in manifest["model_files"]:
            self.remote.write(
                self.folder + "/models/" + name, (ROOT / "models" / name).read_bytes()
            )
        for path in feedback.rglob("*"):
            if path.is_symlink():
                raise ValueError("Feedback symlinks are not allowed")
            if path.is_file():
                self.remote.write(
                    self.home
                    + "/data/feedback/"
                    + path.relative_to(feedback).as_posix(),
                    path.read_bytes(),
                )
        self.remote.python(
            "import json,sys; from pathlib import Path; Path(json.load(sys.stdin)).mkdir(parents=True,exist_ok=True)",
            payload=self.home + "/data/feedback",
        )
        self.remote.write(
            self.folder + "/tools/scripts/start_stack.py",
            (ROOT / "scripts/start_stack.py").read_bytes(),
        )
        self.remote.write(
            self.home + "/backups/" + backup.name, backup.read_bytes(), mode=0o600
        )
        for name, image in dict(
            (s["image"], n) for n, s in config["services"].items()
        ).items():
            print(f"Получение готового образа: {image}", flush=True)
            try:
                self.remote.docker("image", "inspect", name)
            except RuntimeError:
                self.remote.docker("pull", name)
        tls = issue_identity()
        self.remote.docker(
            "volume",
            "create",
            "--label",
            "com.docker.compose.project=" + self.project,
            "--label",
            "devops.vault-tls=" + self.project,
            self.project + "_vault_tls",
        )
        self.helper(
            "import json,os,sys; from pathlib import Path; p=json.load(sys.stdin); r=Path('/tls'); [( (r/n).write_text(v),os.chmod(r/n,0o400 if n.endswith('.key') else 0o444),os.chown(r/n,100,1000)) for n,v in p.items()]",
            data=tls,
            mounts=[f"type=volume,source={self.project}_vault_tls,target=/tls"],
            root=True,
        )
        self.remote.write(self.folder + "/vault/ca.crt", tls["ca.crt"].encode())
        if self.remote.ssh:
            gid = (
                self.remote.python(
                    "import os; print(os.stat('/var/run/docker.sock').st_gid)"
                )
                .decode()
                .strip()
            )
            runtime["services"]["docker-proxy"]["group_add"] = [gid]
        self.remote.write(self.folder + "/runtime.json", json.dumps(runtime).encode())
        key = secrets.token_hex(32)
        store.write({"format": 1, "project": self.project, "recovery_key": key})
        for service, prefix in SERVICES.items():
            self.remote.docker(
                "volume",
                "create",
                "--label",
                "com.docker.compose.project=" + self.project,
                "--label",
                f"devops.vault-identity={self.project}/{service}",
                "--label",
                "com.docker.compose.volume=" + prefix.lower() + "_vault_identity",
                self.project + "_" + prefix.lower() + "_vault_identity",
            )
        self.compose("up", "-d", "--no-deps", "vault")
        for attempt in range(30):
            try:
                initialized = self.vault("sys/init")["initialized"]
                break
            except RuntimeError:
                time.sleep(1)
        else:
            raise RuntimeError("New Vault did not become reachable")
        if initialized:
            raise RuntimeError("Refusing to initialize an existing Vault")
        keys = self.vault(
            "sys/init", {"secret_shares": 1, "secret_threshold": 1}, "PUT"
        )
        bootstrap = {
            "root_token": keys["root_token"],
            "unseal_key": keys["keys_base64"][0],
        }
        encrypted = seal_keys(bootstrap, key, self.project)
        recovery_folder.mkdir(parents=True, exist_ok=True)
        recovery_path = recovery_folder / "vault-recovery.json"
        with recovery_path.open("xb") as stream:
            stream.write(encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        if (
            open_keys(
                recovery_path.read_bytes(), store.read()["recovery_key"], self.project
            )
            != bootstrap
        ):
            raise RuntimeError("Vault recovery could not be verified")
        self.remote.write(self.home + "/vault-recovery.json", encrypted, mode=0o600)
        self.vault("sys/unseal", {"key": bootstrap["unseal_key"]}, "PUT")
        configured = json.loads(
            self.helper(
                "import sys; sys.path.insert(0,'/bootstrap');\n" + VAULT_CONFIGURE,
                data=bootstrap,
                network=self.project + "_default",
                mounts=[
                    f"type=bind,source={self.folder}/tools,target=/bootstrap,readonly",
                    f"type=bind,source={self.folder}/vault/ca.crt,target=/ca.crt,readonly",
                ],
            )
        )
        self.env.update(
            {
                k: configured["database"][k]
                for k in ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")
            }
        )
        for service, prefix in SERVICES.items():
            logical = prefix.lower() + "_vault_identity"
            volume = self.project + "_" + logical
            values = {
                n.lower(): configured["identities"][f"{prefix}_VAULT_{n}"]
                for n in ("ROLE_ID", "SECRET_ID")
            }
            self.helper(
                WRITER,
                data=values,
                mounts=[f"type=volume,source={volume},target=/identity"],
                root=True,
            )
        self.compose(
            "up", "-d", "--no-deps", "--wait", "--wait-timeout", "180", "db", "kafka"
        )
        self.remote.docker(
            "exec",
            "-i",
            self.project + "-db-1",
            "pg_restore",
            "-U",
            "diabetes",
            "-d",
            "diabetes",
            "--exit-on-error",
            "--no-owner",
            "--no-privileges",
            data=backup.read_bytes(),
        )
        from scripts.vps_data import counts

        restored_counts = counts(self.project + "-db-1", remote=self.remote)
        if expected_counts is not None and restored_counts != expected_counts:
            raise RuntimeError("Restored database counts differ from the source export")
        self.remote.write(
            self.home + "/restored-data.json",
            json.dumps(
                {
                    "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
                    "tables": restored_counts,
                }
            ).encode(),
            mode=0o600,
        )
        self.compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--user",
            "0:0",
            "--cap-add",
            "CHOWN",
            "--cap-add",
            "DAC_OVERRIDE",
            "diabetes-api",
            "python",
            "-c",
            "import os; from pathlib import Path; roots=[Path('/app/logs'),Path('/app/data/feedback')]; [os.chown(p,10001,10001,follow_symlinks=False) for r in roots for p in [r,*r.rglob('*')]]",
        )
        volume = self.project + "_alloy_data"
        self.remote.docker(
            "volume",
            "create",
            "--label",
            "com.docker.compose.project=" + self.project,
            volume,
        )
        self.helper(
            "import os; os.chown('/state',473,473)",
            mounts=[f"type=volume,source={volume},target=/state"],
            root=True,
        )
        payload = {
            "database": configured["database"],
            "metrics": configured["metrics"],
            "identities": configured["passwords"],
        }
        for command in (
            ["scripts.update_database"],
            ["provision-runtime"],
            ["src.register_release", "models/current.json", "--apply"],
        ):
            self.compose(
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "diabetes-api",
                "python",
                "-m",
                "scripts.database_maintenance",
                *command,
                data=json.dumps(payload).encode(),
            )
        self.compose(
            "up",
            "-d",
            "--no-build",
            "--pull",
            "never",
            "--wait",
            "--wait-timeout",
            "180",
        )
        self.remote.python(
            "import json,sys,urllib.request; p=json.load(sys.stdin); [urllib.request.urlopen(p+u,timeout=20).close() for u in ('/healthz','/api/db/health')]",
            payload=f"http://127.0.0.1:{self.api_port}",
        )
        self.remote.write(
            self.home + "/current.json",
            json.dumps(
                {
                    "commit": commit,
                    "ci_run_id": str(run_id),
                    "release": self.folder,
                    "database_backup": self.home + "/backups/" + backup.name,
                }
            ).encode(),
            mode=0o600,
        )
        self.remote.python(
            "import json,sys; from pathlib import Path; Path(json.load(sys.stdin)).unlink()",
            payload=self.home + "/bootstrap.pending.json",
        )
        print(
            "Первоначальный запуск завершён. Проверка публичного HTTPS выполняется отдельно.",
            flush=True,
        )


def main():
    import argparse
    from datetime import datetime, timezone

    from scripts.bootstrap_transport import Transport, ssh_connection
    from scripts.deploy_release import deployment_home, deployment_lock
    from scripts.start_stack import KEEPASS_PATH_FILE, KeePassCredentials
    from scripts.vps_data import freeze_export, resume_source

    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--apply", action="store_true")
    action.add_argument("--unseal", action="store_true")
    action.add_argument("--resume-source", type=Path)
    parser.add_argument("--release", type=Path)
    parser.add_argument("--commit")
    parser.add_argument("--ci-run")
    parser.add_argument("--host", default="135.106.211.247")
    parser.add_argument("--user", default="deploy")
    parser.add_argument(
        "--ssh-key", type=Path, default=Path.home() / ".ssh/mloops_cd_ed25519"
    )
    parser.add_argument("--keepass-db", type=Path)
    parser.add_argument("--home", default="/opt/devops_project/deployment")
    parser.add_argument("--origin", default="https://mloops.fun")
    parser.add_argument("--recovery-file", type=Path)
    args = parser.parse_args()
    if args.resume_source:
        resume_source(args.resume_source)
        print(
            "Локальные сервисы запущены. Не используйте две установки для одновременной записи."
        )
        return
    if not re.fullmatch(r"/opt/[a-zA-Z0-9_/-]+", args.home) or ".." in args.home.split(
        "/"
    ):
        parser.error("Use an absolute deployment directory under /opt")
    if args.apply and not all((args.release, args.commit, args.ci_run)):
        parser.error("--apply requires --release, --commit and --ci-run")
    if args.unseal and not args.recovery_file:
        parser.error("--unseal requires --recovery-file")
    remote = Transport(ssh_connection(args.host, args.user, args.ssh_key))
    remote.docker("info", "--format", "{{.ServerVersion}}")
    if args.apply:
        remote.python(
            "import json,sys; from pathlib import Path; p=Path(json.load(sys.stdin)); "
            "assert not (p/'current.json').exists() and not (p/'bootstrap.pending.json').exists(), 'Target is already initialized or pending'",
            payload=args.home,
        )
    keepass = args.keepass_db
    if keepass is None and KEEPASS_PATH_FILE.is_file():
        keepass = Path(KEEPASS_PATH_FILE.read_text(encoding="utf-8").strip())
    if keepass is None:
        parser.error("Provide --keepass-db")
    store = KeePassCredentials(
        keepass, "devops_project-vps", os.getenv("KEEPASS_KEY_FILE")
    )
    if args.apply:
        try:
            existing_key = store.read()
        except RuntimeError:
            existing_key = None
        if existing_key is not None:
            raise RuntimeError(
                "VPS recovery entry already exists; review the previous attempt before retrying"
            )
    bootstrap = Bootstrap(remote, args.home)
    if args.unseal:
        current = json.loads(
            remote.python(
                "import json,sys; from pathlib import Path; print(Path(json.load(sys.stdin)).read_text())",
                payload=args.home + "/current.json",
            )
        )
        bootstrap.folder = current["release"]
        values = open_keys(
            args.recovery_file.read_bytes(),
            store.read()["recovery_key"],
            bootstrap.project,
        )
        reply = bootstrap.vault("sys/unseal", {"key": values["unseal_key"]}, "PUT")
        if reply.get("sealed"):
            raise RuntimeError("Vault is still sealed")
        print("Vault разблокирован.")
        return
    manifest, config = read_release(args.release, args.commit, args.ci_run)
    if inventory(ROOT / "models") != manifest["model_files"]:
        raise ValueError("Local models differ from CI")
    # Download before freezing the source, so registry delays do not prolong downtime.
    for reference in dict.fromkeys(s["image"] for s in config["services"].values()):
        print("Подготовка готового образа на VPS...", flush=True)
        try:
            remote.docker("image", "inspect", reference)
        except RuntimeError:
            remote.docker("pull", reference)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = ROOT / ".local-history/vps-transfer" / ("cutover-" + stamp)
    print(
        "Остановка записи локального приложения и подготовка окончательной копии...",
        flush=True,
    )
    with deployment_lock(deployment_home()):
        backup, feedback, table_counts = freeze_export(folder)
        print(
            f"Копия подготовлена: {folder}. Локальная запись остановлена.", flush=True
        )
        try:
            bootstrap.install(
                args.release,
                backup,
                feedback,
                folder,
                store,
                origin=args.origin,
                commit=args.commit,
                run_id=args.ci_run,
                expected_counts=table_counts,
            )
        except BaseException:
            print(
                f"Перенос не завершён. Данные сохранены в {folder}; повторно --apply не запускайте. Локальные сервисы остаются остановленными до выбора восстановления.",
                flush=True,
            )
            raise
    print(
        f"Зашифрованные ключи: {folder / 'vault-recovery.json'}. Сохраните этот файл вместе с резервной копией KeePass отдельно от VPS.",
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, KeyboardInterrupt) as error:
        raise SystemExit(str(error) or "Операция прервана.") from None
