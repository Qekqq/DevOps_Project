"""Deliver a verified release over host-key-checked SSH, without a VPS runner.

This updates an existing installation only. Bootstrap and database transfer are
separate operator actions; an empty server must never become an empty production
database just because somebody merged a pull request.
"""

import argparse
import os
import re
import shlex
import subprocess
import tarfile
import tempfile
import uuid
from pathlib import Path, PurePosixPath

from scripts.deploy_release import read_release
from scripts.model_delivery import inventory

ROOT = Path(__file__).resolve().parents[1]
REMOTE_HOME = "/opt/devops_project"
PRIVATE_ENV = {"VPS_SSH_KEY", "VPS_SSH_KNOWN_HOSTS", "DOCKERHUB_READ_TOKEN"}
DEPLOY_SCRIPTS = (
    "deploy_release.py",
    "backup_database.py",
    "model_delivery.py",
    "maintenance_client.py",
    "vault_connection.py",
    "vault_identity.py",
)


def build_bundle(output, release, commit, run_id, *, root=ROOT):
    """Only source helpers, verified release files and verified private models."""
    manifest, _ = read_release(release, commit, run_id)
    model_files = inventory(root / "models")
    if model_files != manifest.get("model_files"):
        raise ValueError("Model files differ from the verified release")
    entries = [(root / "scripts" / name, "scripts/" + name) for name in DEPLOY_SCRIPTS]
    entries += [
        (release / name, "release/" + name)
        for name in ["release.json", *manifest["files"]]
    ]
    entries += [(root / "models" / name, "models/" + name) for name in model_files]
    with tarfile.open(output, "w:gz") as archive:
        directories = set()
        for source, name in entries:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
                raise ValueError("Invalid deployment bundle path")
            if source.is_symlink() or not source.is_file():
                raise ValueError("Deployment bundle must contain regular files only")
            # The enclosing stage is 0700. Bind-mounted contents must be readable
            # by the different non-root UIDs inside application/monitoring images.
            for parent in reversed(path.parents):
                if str(parent) == "." or str(parent) in directories:
                    continue
                directory = tarfile.TarInfo(str(parent))
                directory.type = tarfile.DIRTYPE
                directory.mode = 0o755
                archive.addfile(directory)
                directories.add(str(parent))
            info = archive.gettarinfo(str(source), arcname=name)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o644
            with source.open("rb") as stream:
                archive.addfile(info, stream)


def connection(directory, environment):
    host = environment.get("VPS_HOST", "")
    user = environment.get("VPS_USER", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host):
        raise ValueError("Set repository variable VPS_HOST to the VPS address")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*", user):
        raise ValueError("Set repository variable VPS_USER to the deployment user")
    key = environment.get("VPS_SSH_KEY", "").strip()
    hosts = environment.get("VPS_SSH_KNOWN_HOSTS", "").strip()
    if not key.startswith("-----BEGIN OPENSSH PRIVATE KEY-----") or not hosts:
        raise ValueError("VPS_SSH_KEY and VPS_SSH_KNOWN_HOSTS secrets are required")
    key_path, hosts_path = directory / "key", directory / "known_hosts"
    for path, value in ((key_path, key), (hosts_path, hosts)):
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(value.replace("\r\n", "\n") + "\n")
        path.chmod(0o600)
    return [
        "ssh",
        "-T",
        "-F",
        "/dev/null",
        "-i",
        str(key_path),
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={hosts_path}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=4",
        f"{user}@{host}",
    ]


def remote_script(stage, commit, run_id, username):
    # All variable shell values are quoted; none is interpreted as shell code.
    home, target = shlex.quote(REMOTE_HOME), shlex.quote(stage)
    return f"""set -eu
umask 077
export DEVOPS_DEPLOY_HOME={home}/deployment
export DOCKER_CONFIG={target}/docker-auth
cleanup_auth() {{ rm -f -- "$DOCKER_CONFIG/config.json"; rmdir -- "$DOCKER_CONFIG" 2>/dev/null || true; }}
trap cleanup_auth EXIT
mkdir "$DOCKER_CONFIG"
docker login --username {shlex.quote(username)} --password-stdin
cd {target}
python3 -m scripts.deploy_release --release release --commit {shlex.quote(commit)} --ci-run {shlex.quote(run_id)} --preflight
python3 -m scripts.deploy_release --release release --commit {shlex.quote(commit)} --ci-run {shlex.quote(run_id)}
ln -sfn {target} {home}/current-tools
"""


def deploy(release, commit, run_id, *, environment=None, root=ROOT):
    environment = dict(os.environ if environment is None else environment)
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or not re.fullmatch(
        r"[1-9][0-9]*", run_id
    ):
        raise ValueError("A full commit SHA and CI run ID are required")
    username, token = (
        environment.get("DOCKERHUB_USERNAME", ""),
        environment.get("DOCKERHUB_READ_TOKEN", ""),
    )
    if not username or not token:
        raise ValueError("Docker Hub read credentials are required")
    child_env = {
        key: value for key, value in environment.items() if key not in PRIVATE_ENV
    }
    with tempfile.TemporaryDirectory(prefix="devops-vps-") as temporary:
        directory = Path(temporary)
        directory.chmod(0o700)
        ssh = connection(directory, environment)
        bundle = directory / "release.tar.gz"
        build_bundle(bundle, release, commit, run_id, root=root)
        # No installation, containers or application changes during this check.
        preflight = (
            "set -eu; docker info >/dev/null; "
            "python3 -c 'import sys; assert sys.version_info >= (3, 11)'; "
            f"test -f {REMOTE_HOME}/deployment/current.json"
        )
        result = subprocess.run(ssh + [preflight], env=child_env, check=False)
        if result.returncode:
            raise RuntimeError(
                "VPS is not bootstrapped, Docker is unavailable, or SSH validation failed; deployment not started"
            )
        stage = f"{REMOTE_HOME}/incoming/{commit}-{run_id}-{uuid.uuid4().hex[:12]}"
        extract = (
            f"set -eu; umask 077; mkdir -p {REMOTE_HOME}/incoming; "
            f"mkdir {shlex.quote(stage)}; tar --no-same-owner --same-permissions "
            f"-xzf - -C {shlex.quote(stage)}"
        )
        with bundle.open("rb") as source:
            subprocess.run(ssh + [extract], stdin=source, env=child_env, check=True)
        # Token travels only over encrypted stdin, never argv or the release bundle.
        subprocess.run(
            ssh + [remote_script(stage, commit, run_id, username)],
            input=token + "\n",
            text=True,
            env=child_env,
            check=True,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--ci-run", required=True)
    args = parser.parse_args()
    deploy(args.release.resolve(), args.commit, args.ci_run)


if __name__ == "__main__":
    main()
