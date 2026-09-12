import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts import scan_images as scanner


@pytest.mark.parametrize("platform", ["posix", "nt"])
def test_scan_preserves_permissions_and_security_gate(tmp_path, monkeypatch, platform):
    output = tmp_path / "reports"
    monkeypatch.setattr(scanner, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(
        scanner,
        "os",
        SimpleNamespace(name=platform, getuid=lambda: 1001, getgid=lambda: 1002),
    )
    image_id = "sha256:" + "a" * 64
    monkeypatch.setattr(scanner.subprocess, "check_output", lambda *a, **kw: image_id)
    monkeypatch.setattr(scanner, "assess", lambda *a: [])
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        assert kwargs["check"]
        if args[1:3] == ["image", "save"]:
            assert args[-1] == image_id
            (output / "image.tar").write_bytes(b"exported image")
        else:
            assert args[1:3] == ["run", "--rm"]
            if platform == "posix":
                assert args[args.index("--user") + 1] == "1001:1002"
            else:
                assert "--user" not in args
            assert "--cap-drop=ALL" in args
            assert "--read-only" in args
            assert "--security-opt=no-new-privileges" in args
            assert "--privileged" not in args
            assert not any("docker.sock" in arg for arg in args)
            (output / "report.json").write_text(
                json.dumps(
                    {
                        "Results": [
                            {
                                "Target": "library",
                                "Type": "gobinary",
                                "Vulnerabilities": [
                                    {
                                        "VulnerabilityID": "CVE-test",
                                        "PkgName": "library",
                                        "InstalledVersion": "1",
                                        "FixedVersion": "2",
                                        "Severity": "HIGH",
                                    }
                                ],
                            }
                        ]
                    }
                )
            )

    monkeypatch.setattr(scanner.subprocess, "run", run)
    assert not scanner.scan("example/app:release", output)
    assert len(calls) == 2
    assert not (output / "image.tar").exists()
    assert (
        json.loads((output / "report.json").read_text())["ResolvedImageId"] == image_id
    )
    status = json.loads((output / "scan-status.json").read_text())
    assert status["state"] == "complete" and status["blocking"] == 1


def test_scanner_failure_does_not_pass_gate_or_leave_image_archive(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(scanner, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(scanner.subprocess, "check_output", lambda *a, **kw: "image-id")

    def run(args, **kwargs):
        if args[1:3] == ["image", "save"]:
            (tmp_path / "image.tar").write_bytes(b"exported image")
        else:
            raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(scanner.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        scanner.scan("example/app:release", tmp_path)
    assert not (tmp_path / "image.tar").exists()
    status = json.loads((tmp_path / "scan-status.json").read_text())
    assert status["state"] == "failed" and status["error"] == "CalledProcessError"


def test_failed_retry_removes_stale_successful_report(tmp_path, monkeypatch):
    (tmp_path / "report.json").write_text('{"Results": []}')
    monkeypatch.setattr(scanner, "CACHE", tmp_path / "cache")

    def missing_image(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["docker", "inspect"])

    monkeypatch.setattr(scanner.subprocess, "check_output", missing_image)
    with pytest.raises(subprocess.CalledProcessError):
        scanner.scan("missing:tag", tmp_path)
    assert not (tmp_path / "report.json").exists()
    assert json.loads((tmp_path / "scan-status.json").read_text())["state"] == "failed"
