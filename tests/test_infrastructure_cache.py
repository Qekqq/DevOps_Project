import json
from types import SimpleNamespace

import pytest

from scripts import infrastructure_cache as cache


@pytest.fixture
def root(tmp_path):
    (tmp_path / "revision.txt").write_text("1")
    for name, files in cache.INPUTS.items():
        (tmp_path / name).mkdir()
        for file in files:
            (tmp_path / name / file).write_text("initial build input")
    return tmp_path


def test_application_or_audit_edit_does_not_require_infrastructure_migration(root):
    before = {name: cache.fingerprint(name, root) for name in cache.INPUTS}
    (root / "frontend.js").write_text("new application text")
    (root / "vault/REVIEW.md").write_text("updated audit notes")
    assert before == {name: cache.fingerprint(name, root) for name in cache.INPUTS}
    (root / "kafka/artifacts.lock.json").write_text("new dependencies")
    assert cache.fingerprint("kafka", root) != before["kafka"]
    assert cache.fingerprint("vault", root) == before["vault"]
    (root / "revision.txt").write_text("2")
    assert all(cache.fingerprint(name, root) != before[name] for name in cache.INPUTS)


def test_new_build_context_file_cannot_be_silently_omitted_from_identity(root):
    (root / "kafka/new_dependency.py").write_text("new build dependency")
    with pytest.raises(ValueError, match="input list"):
        cache.fingerprint("kafka", root)


def test_windows_line_endings_reuse_the_same_published_recipe(root):
    path = root / "vault/Dockerfile"
    path.write_bytes(b"FROM base\nRUN build\n")
    expected = cache.fingerprint("vault", root)
    path.write_bytes(b"FROM base\r\nRUN build\r\n")
    assert cache.fingerprint("vault", root) == expected


def test_application_resolves_existing_recipes_without_build_pull_or_scan(
    root, monkeypatch
):
    monkeypatch.setattr(cache, "registry_digest", lambda _: "sha256:" + "a" * 64)
    monkeypatch.setattr(cache, "execute", lambda *a: pytest.fail("No Docker mutations"))
    monkeypatch.setattr(
        cache, "scan", lambda *a: pytest.fail("No infrastructure rescan")
    )
    state = cache.resolve("example/app", root=root)
    assert set(state["images"]) == set(cache.INPUTS)
    assert all(
        i["reference"] == "example/app@sha256:" + "a" * 64
        for i in state["images"].values()
    )
    with pytest.raises(ValueError, match="not a prepared"):
        cache.validate(state, root=root)
    with pytest.raises(ValueError, match="before publication"):
        cache.publish(state, root=root)


def test_missing_published_recipe_requires_manual_workflow_without_fallback(
    root, monkeypatch
):
    monkeypatch.setattr(cache, "registry_digest", lambda _: None)
    monkeypatch.setattr(
        cache, "execute", lambda *a: pytest.fail("Must not build or pull")
    )
    with pytest.raises(RuntimeError, match="Infrastructure workflow"):
        cache.resolve("example/app", root=root)


def test_monitoring_group_tracks_its_own_builds_and_rejects_other_group(
    tmp_path, monkeypatch
):
    (tmp_path / "revision.txt").write_text("1")
    for name, paths in cache.MONITORING_INPUTS.items():
        for relative in paths:
            path = tmp_path / name / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("build input")
    options = {"root": tmp_path, "inputs": cache.MONITORING_INPUTS}
    monkeypatch.setattr(cache, "registry_digest", lambda _: None)
    monkeypatch.setattr(cache, "local_id", lambda _: "sha256:" + "a" * 64)
    calls = []
    monkeypatch.setattr(cache, "execute", lambda *args: calls.append(args))
    state = cache.prepare("example/app", **options)
    assert set(state["images"]) == {"grafana", "prometheus", "loki", "alloy"}
    assert not any(c[0] in {"builder", "volume", "system"} for c in calls)
    cache.validate(state, **options)
    with pytest.raises(ValueError, match="Incomplete"):
        cache.validate(state, root=tmp_path)
    (tmp_path / "loki/Dockerfile").write_text("updated dependency")
    with pytest.raises(ValueError, match="inputs changed"):
        cache.validate(state, **options)


def test_build_cache_cleanup_requires_explicit_option(root, monkeypatch):
    calls = []
    monkeypatch.setattr(cache, "registry_digest", lambda _: None)
    monkeypatch.setattr(cache, "local_id", lambda _: "sha256:" + "a" * 64)
    monkeypatch.setattr(cache, "execute", lambda *args: calls.append(args))
    cache.prepare("example/app", root=root, prune_build_cache=True)
    assert calls.count(("builder", "prune", "--all", "--force")) == len(cache.INPUTS)
    assert not any(c[0] in {"volume", "system", "image"} for c in calls)


@pytest.mark.parametrize(
    "message", ["unauthorized", "429 Too Many Requests", "connection timed out"]
)
def test_registry_failure_is_not_a_cache_miss(monkeypatch, message):
    monkeypatch.setattr(
        cache.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=1, stderr=message),
    )
    with pytest.raises(RuntimeError):
        cache.registry_digest("example/app:infra-vault")


def test_explicit_missing_manifest_can_be_built(monkeypatch):
    reference = "example/app:infra-vault"
    monkeypatch.setattr(
        cache.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            returncode=1, stderr=f"ERROR: {reference}: not found\n"
        ),
    )
    assert cache.registry_digest(reference) is None


def test_existing_images_are_reused_by_digest_without_rebuilding(root, monkeypatch):
    digests = {
        name: "sha256:" + str(index) * 64 for index, name in enumerate(cache.INPUTS, 1)
    }
    calls = []

    def lookup(reference):
        return next(
            digest
            for name, digest in digests.items()
            if ":infra-" + name + "-" in reference
        )

    def execute(*args):
        calls.append(args)
        if args[0] == "pull":
            return ""
        assert args[:2] == ("image", "inspect")
        name = next(
            name for name, digest in digests.items() if args[2].endswith("@" + digest)
        )
        if args[-1] == "{{.Id}}":
            return digests[name]
        return json.dumps({cache.LABEL: cache.fingerprint(name, root)})

    monkeypatch.setattr(cache, "registry_digest", lookup)
    monkeypatch.setattr(cache, "execute", execute)
    first = cache.prepare("example/app", root=root)
    second = cache.prepare("example/app", root=root)
    assert first == second and not first["scanned"]
    assert all(not item["new"] for item in first["images"].values())
    assert not any(args[0] in {"build", "push"} for args in calls)


def test_failed_rescan_clears_previous_success(root, monkeypatch):
    state = {"scanned": True, "images": {"vault": {"reference": "image"}}}
    monkeypatch.setattr(cache, "validate", lambda *a, **kw: None)
    monkeypatch.setattr(cache, "scan", lambda *a: False)
    with pytest.raises(RuntimeError):
        cache.scan_prepared(state, root, root=root)
    assert not state["scanned"]
    with pytest.raises(ValueError):
        cache.publish(state, root=root)


def test_publication_never_overwrites_tag_created_by_another_run(root, monkeypatch):
    state = {
        "scanned": True,
        "images": {"vault": {"new": True, "tag": "example/app:infra-vault"}},
    }
    monkeypatch.setattr(cache, "validate", lambda *a, **kw: None)
    monkeypatch.setattr(cache, "registry_digest", lambda _: "sha256:" + "a" * 64)
    monkeypatch.setattr(cache, "execute", lambda *a: pytest.fail("Must not push"))
    with pytest.raises(RuntimeError, match="appeared"):
        cache.publish(state, root=root)
