"""Verify actual runtime boundaries, not just Compose declarations."""

import importlib.util
import os
from pathlib import Path

import pytest
import requests


def test_runtime_is_nonroot_readonly_and_has_no_build_tools():
    assert os.getuid() == 10001
    for module in ("pip", "setuptools", "src.train", "src.data_preprocessing"):
        assert importlib.util.find_spec(module) is None
    with pytest.raises(OSError):
        Path("/app/security-write-probe").write_text("probe")
    temporary = Path("/tmp/security-write-probe")
    temporary.write_text("probe")
    temporary.unlink()
    model_mount = next(
        line.split()
        for line in Path("/proc/mounts").read_text().splitlines()
        if line.split()[1] == "/app/models"
    )
    assert "ro" in model_mount[3].split(",")


def test_edge_rejects_large_requests_and_throttles_login():
    base = "http://frontend:8080"
    response = requests.post(base + "/api/auth/login", data="x" * 40000, timeout=10)
    assert response.status_code == 413
    statuses = [
        requests.post(base + "/api/auth/login", json={}, timeout=10).status_code
        for _ in range(12)
    ]
    assert 429 in statuses
    assert requests.get(base + "/healthz", timeout=5).status_code == 200
