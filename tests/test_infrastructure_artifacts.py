import hashlib
import io
import json

import pytest

from infrastructure.kafka.fetch_artifacts import fetch_artifacts


def artifact(tmp_path, **changes):
    record = {
        "file": "example-1.0.jar",
        "url": "https://repo.maven.apache.org/maven2/example/example-1.0.jar",
        "sha512": hashlib.sha512(b"reviewed jar").hexdigest(),
    }
    record.update(changes)
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps([record]), encoding="utf-8")
    return lock


def test_downloaded_jar_must_match_reviewed_checksum(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *args, **kwargs: io.BytesIO(b"tampered jar")
    )
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="checksum mismatch"):
        fetch_artifacts(artifact(tmp_path), output)
    assert not list(output.iterdir())


@pytest.mark.parametrize(
    "changes",
    [
        {"url": "http://repo.maven.apache.org/maven2/example/example-1.0.jar"},
        {"url": "https://repo.maven.apache.org.attacker.test/example-1.0.jar"},
        {"url": "file:///etc/example-1.0.jar"},
        {"file": "../example-1.0.jar"},
    ],
)
def test_invalid_origin_or_filename_is_rejected_before_request(
    tmp_path, monkeypatch, changes
):
    def forbidden(*args, **kwargs):
        pytest.fail("An invalid artifact must not trigger a request")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    with pytest.raises(ValueError, match="Invalid artifact"):
        fetch_artifacts(artifact(tmp_path, **changes), tmp_path / "output")


def test_verified_bytes_are_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *args, **kwargs: io.BytesIO(b"reviewed jar")
    )
    output = tmp_path / "output"
    fetch_artifacts(artifact(tmp_path), output)
    assert (output / "example-1.0.jar").read_bytes() == b"reviewed jar"
