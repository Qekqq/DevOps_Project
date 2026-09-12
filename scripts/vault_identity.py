"""Store AppRole identities in service-specific Docker volumes, never host files."""

import json
import re
import subprocess

SERVICES = {
    "diabetes-api": "API",
    "kafka-consumer": "CONSUMER",
    "metrics-exporter": "EXPORTER",
}
TARGET = "/run/vault-identity"
WRITER = """
import json, os, sys
from pathlib import Path
from uuid import uuid4
root=Path('/identity')
values=json.load(sys.stdin)
if set(values) != {'role_id','secret_id'} or any(not isinstance(v,str) or not v.strip() for v in values.values()):
    raise ValueError('Invalid identity')
for name, value in values.items():
    path=root / (name + '.' + uuid4().hex + '.tmp')
    fd=os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchown(stream.fileno(),10001,10001)
        os.replace(path,root / name)
    finally:
        path.unlink(missing_ok=True)
os.chown(root,10001,10001)
os.chmod(root,0o500)
"""


def write_identities(project, image, identities, *, replace=False):
    """Only call replace after stopping clients; both identity files must rotate together."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]+", project):
        raise ValueError("Invalid Compose project name")
    overlay = {"services": {}, "volumes": {}}
    for service, prefix in SERVICES.items():
        logical = prefix.lower() + "_vault_identity"
        volume = project + "_" + logical
        values = {
            suffix.lower(): identities[f"{prefix}_VAULT_{suffix}"]
            for suffix in ("ROLE_ID", "SECRET_ID")
        }
        if not all(
            isinstance(value, str) and value.strip() for value in values.values()
        ):
            raise ValueError("Empty AppRole identity")
        result = subprocess.run(
            ["docker", "volume", "inspect", volume], capture_output=True, text=True
        )
        if result.returncode == 0:
            labels = json.loads(result.stdout)[0].get("Labels") or {}
            if (
                not replace
                or labels.get("devops.vault-identity") != project + "/" + service
            ):
                raise RuntimeError(
                    "Refusing to overwrite an existing or foreign AppRole volume"
                )
        else:
            subprocess.run(
                [
                    "docker",
                    "volume",
                    "create",
                    "--label",
                    f"devops.vault-identity={project}/{service}",
                    "--label",
                    f"com.docker.compose.project={project}",
                    "--label",
                    f"com.docker.compose.volume={logical}",
                    volume,
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-i",
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
                "no-new-privileges:true",
                "--memory",
                "128m",
                "--cpus",
                "1",
                "--mount",
                f"type=volume,source={volume},target=/identity",
                "--entrypoint",
                "python",
                image,
                "-c",
                WRITER,
            ],
            input=json.dumps(values),
            text=True,
            capture_output=True,
        )
        if result.returncode:
            raise RuntimeError(
                "Cannot write AppRole identity; private diagnostics suppressed"
            )
        overlay["volumes"][logical] = {"name": volume}
        overlay["services"][service] = {
            "environment": {
                "VAULT_ROLE_ID": "",
                "VAULT_SECRET_ID": "",
                "VAULT_ROLE_ID_FILE": TARGET + "/role_id",
                "VAULT_SECRET_ID_FILE": TARGET + "/secret_id",
            },
            "volumes": [
                {
                    "type": "volume",
                    "source": logical,
                    "target": TARGET,
                    "read_only": True,
                }
            ],
        }
    return overlay


def preserve_identities(config, services, project):
    """Preserve only each client's own read-only identity mount, without reading secrets."""
    for service, prefix in SERVICES.items():
        values = dict(
            item.split("=", 1)
            for item in services[service]["Config"]["Env"]
            if "=" in item
        )
        paths = [
            values.get("VAULT_" + suffix + "_FILE")
            for suffix in ("ROLE_ID", "SECRET_ID")
        ]
        target = config["services"][service].setdefault("environment", {})
        if not any(paths):
            target.pop("VAULT_ROLE_ID_FILE", None)
            target.pop("VAULT_SECRET_ID_FILE", None)
            continue
        if paths != [TARGET + "/role_id", TARGET + "/secret_id"]:
            raise RuntimeError("Unexpected installed AppRole file paths")
        mounts = [
            m for m in services[service].get("Mounts", []) if m["Destination"] == TARGET
        ]
        logical = prefix.lower() + "_vault_identity"
        volume = project + "_" + logical
        if (
            len(mounts) != 1
            or mounts[0]["Type"] != "volume"
            or mounts[0]["RW"]
            or mounts[0].get("Name") != volume
        ):
            raise RuntimeError("AppRole requires the service's own read-only volume")
        config.setdefault("volumes", {})[logical] = {"name": volume, "external": True}
        entries = config["services"][service].setdefault("volumes", [])
        entries[:] = [m for m in entries if m["target"] != TARGET]
        entries.append(
            {"type": "volume", "source": logical, "target": TARGET, "read_only": True}
        )
        for suffix, path in zip(("ROLE_ID", "SECRET_ID"), paths):
            target.pop("VAULT_" + suffix, None)
            target["VAULT_" + suffix + "_FILE"] = path
