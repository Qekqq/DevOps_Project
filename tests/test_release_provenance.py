import hashlib
import io
import json
import zipfile

import pytest

from scripts import release_provenance
from scripts.release_provenance import REPOSITORY, compare_archive, validate_run


@pytest.fixture
def release(tmp_path):
    content = b'{"services":{}}'
    manifest = {
        "commit": "a" * 40,
        "ci_run_id": "123",
        "files": {"docker-compose.json": hashlib.sha256(content).hexdigest()},
    }
    (tmp_path / "docker-compose.json").write_bytes(content)
    (tmp_path / "release.json").write_text(json.dumps(manifest))
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("release.json", json.dumps(manifest))
        archive.writestr("docker-compose.json", content)
    return tmp_path, manifest, stream.getvalue()


def test_exact_github_archive_matches_local_package(release):
    folder, manifest, data = release
    compare_archive(data, folder, manifest)


def test_locally_modified_package_cannot_claim_github_provenance(release):
    folder, manifest, data = release
    (folder / "docker-compose.json").write_bytes(b"changed")
    manifest["files"]["docker-compose.json"] = hashlib.sha256(b"changed").hexdigest()
    with pytest.raises(ValueError):
        compare_archive(data, folder, manifest)


def test_extra_local_configuration_is_rejected(release):
    folder, manifest, data = release
    (folder / "injected.conf").write_text("unverified")
    with pytest.raises(ValueError, match="unverified"):
        compare_archive(data, folder, manifest)


def test_network_failure_does_not_disclose_signed_download_url(release, monkeypatch):
    import traceback

    secret = "https://artifact.example/file.zip?signature=private-test-signature"

    def fail(*args):
        raise release_provenance.requests.ConnectionError(secret)

    monkeypatch.setattr(release_provenance, "_verify_source", fail)
    with pytest.raises(RuntimeError) as error:
        release_provenance.verify_source(release[0], release[1])
    assert secret not in "".join(traceback.format_exception(error.value))


def test_github_token_is_not_forwarded_to_artifact_storage(release, monkeypatch):
    from unittest.mock import MagicMock

    folder, manifest, data = release
    run = {
        "id": 123,
        "path": ".github/workflows/ci.yml",
        "event": "push",
        "conclusion": "success",
        "status": "completed",
        "head_sha": manifest["commit"],
        "head_branch": "main",
        "head_repository": {"full_name": REPOSITORY},
    }
    artifact = {
        "id": 42,
        "name": "release-" + manifest["commit"],
        "expired": False,
        "size_in_bytes": len(data),
        "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
    }
    api, storage = MagicMock(), MagicMock()
    for session in (api, storage):
        session.headers = {}
        session.__enter__.return_value = session
    responses = []
    for payload in (
        run,
        {"commit": {"sha": manifest["commit"]}},
        {"total_count": 1, "artifacts": [artifact]},
    ):
        response = MagicMock(status_code=200)
        response.json.return_value = payload
        responses.append(response)
    responses.append(
        MagicMock(
            status_code=302,
            headers={"Location": "https://artifact.example/file.zip?signature=test"},
        )
    )
    api.get.side_effect = responses
    downloaded = MagicMock(status_code=200)
    downloaded.__enter__.return_value = downloaded
    downloaded.iter_content.return_value = [data]
    storage.get.return_value = downloaded
    monkeypatch.setattr(
        release_provenance.requests, "Session", MagicMock(side_effect=[api, storage])
    )
    monkeypatch.setattr(
        release_provenance, "github_token", lambda: "private-test-token"
    )
    result = release_provenance.verify_source(folder, manifest)
    assert result["artifact_id"] == 42
    assert api.headers["Authorization"] == "Bearer private-test-token"
    assert not storage.headers and not storage.trust_env
    assert "private-test-token" not in str(storage.get.call_args)
    assert not storage.get.call_args.kwargs["allow_redirects"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("head_branch", "develop"),
        ("event", "pull_request"),
        ("conclusion", "failure"),
        ("head_sha", "b" * 40),
        ("head_repository", {"full_name": "attacker/fork"}),
        ("path", ".github/workflows/other.yml"),
    ],
)
def test_rejects_untrusted_or_unsuccessful_run(release, field, value):
    _, manifest, _ = release
    run = {
        "id": 123,
        "path": ".github/workflows/ci.yml",
        "event": "push",
        "conclusion": "success",
        "status": "completed",
        "head_sha": manifest["commit"],
        "head_branch": "main",
        "head_repository": {"full_name": REPOSITORY},
    }
    branch = {"commit": {"sha": manifest["commit"]}}
    validate_run(run, branch, manifest)
    run[field] = value
    with pytest.raises(ValueError):
        validate_run(run, branch, manifest)
