"""Verify a local package against the artifact of successful CI on current main."""

import hashlib
import io
import json
import os
import re
import subprocess
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import requests

REPOSITORY = "Qekqq/DevOps_Project"
LIMIT = 100 * 1024 * 1024


def validate_run(run, branch, manifest):
    if not (
        run.get("path") == ".github/workflows/ci.yml"
        and run.get("conclusion") == "success"
        and run.get("status") == "completed"
        and run.get("event") in {"push", "workflow_dispatch"}
        and run.get("head_branch") == "main"
        and run.get("head_repository", {}).get("full_name", "").lower()
        == REPOSITORY.lower()
        and run.get("head_sha") == branch["commit"]["sha"] == manifest["commit"]
        and str(run.get("id")) == str(manifest["ci_run_id"])
    ):
        raise ValueError("Release is not successful CI of the current main branch")


def compare_archive(data, folder, manifest):
    if len(data) > LIMIT:
        raise ValueError("Release archive is too large")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        files = [item for item in archive.infolist() if not item.is_dir()]
        names = [item.filename for item in files]
        if (
            len(set(names)) != len(names)
            or sum(item.file_size for item in files) > LIMIT
        ):
            raise ValueError("Duplicate files or oversized release")
        expected = {*manifest["files"], "release.json"}
        if set(names) != expected:
            raise ValueError("GitHub artifact inventory differs from the local package")
        entries = list(folder.rglob("*"))
        if (
            any(p.is_symlink() for p in entries)
            or {p.relative_to(folder).as_posix() for p in entries if p.is_file()}
            != expected
        ):
            raise ValueError("Local package contains unverified files or links")
        for item in files:
            name = item.filename
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or "\\" in name:
                raise ValueError("Unsafe release path")
            local = (folder / name).resolve(strict=True)
            if not local.is_relative_to(folder.resolve()) or local.is_symlink():
                raise ValueError("Local release path escapes the package")
            content = archive.read(item)
            if name == "release.json":
                if json.loads(content) != manifest:
                    raise ValueError("Release manifest does not match GitHub")
            elif (
                hashlib.sha256(content).hexdigest() != manifest["files"][name]
                or content != local.read_bytes()
            ):
                raise ValueError("Release content does not match GitHub")


def github_token():
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    if token:
        return token
    try:
        result = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "Install GitHub CLI and run gh auth login, or set GH_TOKEN with read access to Actions artifacts"
        ) from None
    if result.returncode or not result.stdout.strip():
        raise RuntimeError(
            "Authenticate GitHub CLI with read access to Actions artifacts"
        )
    return result.stdout.strip()


def verify_source(folder, manifest):
    try:
        return _verify_source(folder, manifest)
    except requests.RequestException:
        # Requests errors can contain the signed artifact URL (a temporary
        # credential). Never include it or the chained exception in diagnostics.
        raise RuntimeError("GitHub verification or artifact download failed") from None


def _verify_source(folder, manifest):
    run_id = str(manifest["ci_run_id"])
    if not re.fullmatch(r"[1-9][0-9]*", run_id):
        raise ValueError("Invalid CI run identifier")
    base = f"https://api.github.com/repos/{REPOSITORY}"
    with requests.Session() as session:
        session.trust_env = False
        session.headers.update(
            {
                "Authorization": "Bearer " + github_token(),
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
            }
        )

        def get(path):
            response = session.get(base + path, timeout=30, allow_redirects=False)
            if response.status_code != 200:
                raise RuntimeError("GitHub release verification failed")
            return response.json()

        validate_run(get(f"/actions/runs/{run_id}"), get("/branches/main"), manifest)
        artifacts = get(f"/actions/runs/{run_id}/artifacts?per_page=100")
        if artifacts["total_count"] > 100:
            raise ValueError("Unexpected number of release artifacts")
        matching = [
            a
            for a in artifacts["artifacts"]
            if a["name"] == f"release-{manifest['commit']}" and not a["expired"]
        ]
        if len(matching) != 1:
            raise ValueError("Exactly one unexpired release artifact is required")
        artifact = matching[0]
        if artifact["size_in_bytes"] > LIMIT or type(artifact["id"]) is not int:
            raise ValueError("Invalid release artifact")
        response = session.get(
            base + f"/actions/artifacts/{artifact['id']}/zip",
            timeout=30,
            allow_redirects=False,
        )
        location = response.headers.get("Location", "")
        parsed = urlsplit(location)
        if (
            response.status_code != 302
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Invalid GitHub artifact download location")
    # GitHub's short-lived signed storage URL receives no GitHub token/cookies.
    with requests.Session() as download:
        download.trust_env = False
        with download.get(
            location, timeout=60, stream=True, allow_redirects=False
        ) as response:
            if response.status_code != 200:
                raise RuntimeError("Artifact download failed")
            data = bytearray()
            for chunk in response.iter_content(1024 * 1024):
                data.extend(chunk)
                if len(data) > LIMIT:
                    raise ValueError("Release archive is too large")
    digest = artifact.get("digest")
    if not digest or digest != "sha256:" + hashlib.sha256(data).hexdigest():
        raise ValueError("GitHub artifact digest is absent or does not match")
    compare_archive(data, Path(folder), manifest)
    return {
        "repository": REPOSITORY,
        "ci_run_id": run_id,
        "commit": manifest["commit"],
        "artifact_id": artifact["id"],
        "artifact_digest": digest,
    }
