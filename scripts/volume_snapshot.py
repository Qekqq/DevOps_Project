"""Verified cold-volume backups for an explicit infrastructure migration.

This module never stops containers or changes the source volume. Callers must
hold the deployment lock and stop all writers before invoking create_snapshot.
"""

import json
import re
import subprocess
from datetime import datetime, timezone
from uuid import uuid4

SERVICE_VOLUMES = {"db": "pgdata", "vault": "vault_data", "kafka": "kafka_data"}

COPY = """
import hashlib, json, os, shutil, sys, tarfile
from pathlib import Path
metadata=json.load(sys.stdin)
root=Path('/backup')
archive=root/'data.tar'
if any(root.iterdir()):
    raise RuntimeError('Backup volume must be empty')
# Tar records round file sizes to 512 bytes and add headers. Reserve room for
# PAX headers and unrelated service writes; reject before creating an archive.
required=10240 + 256*1024*1024
for path in [Path('/source'), *Path('/source').rglob('*')]:
    stat=path.lstat()
    if path.is_symlink() or not (path.is_file() or path.is_dir()):
        raise RuntimeError('Unsupported source entry')
    required += ((stat.st_size + 511)//512)*512 + 4096
if shutil.disk_usage(root).free < required:
    raise RuntimeError('Insufficient space for snapshot and safety reserve')
def checked(member):
    if not (member.isfile() or member.isdir()) or member.mode & 0o6000:
        raise RuntimeError('Links, special files and privileged modes are not supported')
    return member
os.umask(0o077)
with archive.open('xb') as stream:
    with tarfile.open(fileobj=stream,mode='w') as output:
        output.add('/source',arcname='data',filter=checked)
    stream.flush()
    os.fsync(stream.fileno())
with archive.open('rb') as stream:
    digest=hashlib.file_digest(stream,'sha256').hexdigest()
with tarfile.open(archive,'r:') as source:
    members=source.getmembers()
    for member in members:
        checked(member)
metadata.update(sha256=digest,archive_bytes=archive.stat().st_size,entries=len(members))
with (root/'pending.json').open('x',encoding='utf-8') as stream:
    json.dump(metadata,stream)
    stream.flush()
    os.fsync(stream.fileno())
print(json.dumps(metadata))
"""
PUBLISH = """
import hashlib, json, os, sys
from pathlib import Path
root=Path('/backup')
expected=json.load(sys.stdin)
if json.loads((root/'pending.json').read_text()) != expected or (root/'manifest.json').exists():
    raise RuntimeError('Snapshot manifest mismatch')
with (root/'data.tar').open('rb') as stream:
    if hashlib.file_digest(stream,'sha256').hexdigest() != expected['sha256']:
        raise RuntimeError('Snapshot checksum mismatch')
os.chmod(root/'data.tar',0o400)
os.chmod(root/'pending.json',0o400)
os.replace(root/'pending.json',root/'manifest.json')
if os.name == 'posix':
    fd=os.open(root,os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
"""
VERIFY = """
import hashlib, json, sys, tarfile
from pathlib import Path, PurePosixPath
root=Path('/backup')
expected=json.load(sys.stdin)
if json.loads((root/'manifest.json').read_text()) != expected:
    raise RuntimeError('Snapshot manifest mismatch')
archive=root/'data.tar'
with archive.open('rb') as stream:
    if hashlib.file_digest(stream,'sha256').hexdigest() != expected['sha256']:
        raise RuntimeError('Snapshot checksum mismatch')
with tarfile.open(archive,'r:') as source:
    members=source.getmembers()
    if len(members) != expected['entries'] or archive.stat().st_size != expected['archive_bytes']:
        raise RuntimeError('Snapshot inventory mismatch')
    for member in members:
        path=PurePosixPath(member.name)
        if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != 'data' or not (member.isfile() or member.isdir()) or member.mode & 0o6000:
            raise RuntimeError('Unsafe snapshot archive member')
"""


class SnapshotError(RuntimeError):
    def __init__(self, message, *, volume=None):
        super().__init__(message)
        self.volume = volume


def docker(*arguments, stdin=None):
    result = subprocess.run(
        ["docker", *arguments],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=600,
    )
    if result.returncode:
        raise SnapshotError(
            f"Snapshot operation failed during Docker {arguments[0]}; private diagnostics suppressed"
        )
    return result.stdout.strip()


def stopped_writers(source):
    ids = docker("ps", "-aq", "--filter", "volume=" + source).split()
    states = {}
    if ids:
        for container in json.loads(docker("inspect", *ids)):
            if not any(
                m.get("Name") == source and m.get("RW") for m in container["Mounts"]
            ):
                continue
            state = container["State"]
            if state["Running"] or state.get("Paused") or state.get("Restarting"):
                raise SnapshotError(
                    "Source volume has an active writer; stop it before taking a snapshot"
                )
            states[container["Id"]] = {
                key: state.get(key) for key in ("StartedAt", "FinishedAt", "Status")
            }
    return states


def helper(image_id, backup, program, metadata, *, source=None, writable=False):
    arguments = [
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
        "--security-opt",
        "no-new-privileges:true",
        "--memory",
        "256m",
        "--cpus",
        "1",
        "--pids-limit",
        "32",
    ]
    if source:
        arguments += [
            "--cap-add",
            "DAC_READ_SEARCH",
            "--mount",
            f"type=volume,source={source},target=/source,readonly",
        ]
    arguments += [
        "--mount",
        f"type=volume,source={backup},target=/backup"
        + ("" if writable else ",readonly"),
    ]
    return docker(
        *arguments,
        "--entrypoint",
        "python",
        image_id,
        "-c",
        program,
        stdin=json.dumps(metadata),
    )


def create_snapshot(project, service, source, helper_image):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]+", project) or service not in {
        "db",
        "vault",
        "kafka",
    }:
        raise ValueError("Invalid snapshot project or infrastructure service")
    info = json.loads(docker("volume", "inspect", source))[0]
    if (info.get("Labels") or {}).get("com.docker.compose.project") != project:
        raise SnapshotError("Source volume belongs to another project")
    if (info.get("Labels") or {}).get("com.docker.compose.volume") != SERVICE_VOLUMES[
        service
    ]:
        raise SnapshotError(
            "Source volume does not belong to this infrastructure service"
        )
    if info.get("Driver") != "local" or info.get("Options"):
        raise SnapshotError("Only ordinary local Docker volumes are supported")
    writers = stopped_writers(source)
    image_id = json.loads(docker("image", "inspect", helper_image))[0]["Id"]
    backup = f"{project}_snapshot_{service}_{uuid4().hex[:16]}"
    metadata = {
        "format": 1,
        "project": project,
        "service": service,
        "source": source,
        "volume": backup,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "helper_image": image_id,
        "writers": writers,
    }
    docker(
        "volume",
        "create",
        "--label",
        "devops.snapshot.project=" + project,
        "--label",
        "devops.snapshot.source=" + source,
        "--label",
        "devops.snapshot.service=" + service,
        backup,
    )
    try:
        metadata = json.loads(
            helper(image_id, backup, COPY, metadata, source=source, writable=True)
        )
        if stopped_writers(source) != writers:
            raise SnapshotError(
                "Source containers changed during snapshot; backup must not be used"
            )
        verify_snapshot(metadata, pending=True)
        if stopped_writers(source) != writers:
            raise SnapshotError("Source containers changed before snapshot publication")
        helper(image_id, backup, PUBLISH, metadata, writable=True)
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        raise SnapshotError(
            f"Snapshot is incomplete and must not be used: {backup}", volume=backup
        ) from error
    return metadata


def verify_snapshot(metadata, *, pending=False, helper_image=None):
    volume = metadata["volume"]
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]+", volume) or not re.fullmatch(
        r"sha256:[a-f0-9]{64}", metadata["helper_image"]
    ):
        raise SnapshotError("Invalid snapshot reference")
    info = json.loads(docker("volume", "inspect", volume))[0]
    labels = info.get("Labels") or {}
    expected = {
        "devops.snapshot.project": metadata["project"],
        "devops.snapshot.source": metadata["source"],
        "devops.snapshot.service": metadata["service"],
    }
    if any(labels.get(key) != value for key, value in expected.items()):
        raise SnapshotError("Snapshot volume ownership mismatch")
    program = VERIFY.replace("manifest.json", "pending.json") if pending else VERIFY
    image_id = (
        json.loads(docker("image", "inspect", helper_image))[0]["Id"]
        if helper_image
        else metadata["helper_image"]
    )
    helper(image_id, volume, program, metadata)


def restore_snapshot_to_new_volume(metadata, *, helper_image=None):
    """Restore only into a newly generated volume; never overwrite an existing one."""
    verify_snapshot(metadata, helper_image=helper_image)
    image_id = (
        json.loads(docker("image", "inspect", helper_image))[0]["Id"]
        if helper_image
        else metadata["helper_image"]
    )
    project, service = metadata["project"], metadata["service"]
    if (
        not re.fullmatch(r"[a-z0-9][a-z0-9_-]+", project)
        or service not in SERVICE_VOLUMES
    ):
        raise SnapshotError("Invalid restore project or service")
    target = f"{project}_restore_{service}_{uuid4().hex[:16]}"
    docker(
        "volume",
        "create",
        "--label",
        "com.docker.compose.project=" + project,
        "--label",
        "com.docker.compose.volume=" + SERVICE_VOLUMES[service],
        "--label",
        "devops.restore.source=" + metadata["volume"],
        target,
    )
    program = (
        VERIFY
        + """
import os, shutil
restore=Path('/restore')
if any(restore.iterdir()):
    raise RuntimeError('Restore target must be empty')
if shutil.disk_usage(restore).free < expected['archive_bytes'] + 256*1024*1024:
    raise RuntimeError('Insufficient space for restore and safety reserve')
with tarfile.open(archive,'r:') as source:
    members=source.getmembers()
    for member in members:
        member.name='.' if member.name == 'data' else member.name.removeprefix('data/')
    # VERIFY checked the checksum and rejected links, traversal and special files.
    # Preserve original UID/GID and modes for the stopped database cluster.
    source.extractall(restore,members=members,filter='fully_trusted')
for path in [*restore.rglob('*'),restore]:
    fd=os.open(path,os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
"""
    )
    try:
        docker(
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
            "256m",
            "--cpus",
            "1",
            "--pids-limit",
            "32",
            "--mount",
            f"type=volume,source={metadata['volume']},target=/backup,readonly",
            "--mount",
            f"type=volume,source={target},target=/restore",
            "--entrypoint",
            "python",
            image_id,
            "-c",
            program,
            stdin=json.dumps(metadata),
        )
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        raise SnapshotError(
            f"Restore is incomplete; do not attach this volume: {target}", volume=target
        ) from error
    return target
