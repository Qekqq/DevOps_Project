"""Reuse scanned infrastructure digests until their reviewed build inputs change."""

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

from scripts.scan_images import scan

ROOT = Path(__file__).resolve().parents[1] / "infrastructure"
INPUTS = {
    "postgres": {"Dockerfile"},
    "vault": {"Dockerfile"},
    "kafka": {"Dockerfile", "artifacts.lock.json", "fetch_artifacts.py"},
}
MONITORING_INPUTS = {
    "grafana": {
        "Dockerfile",
        "provisioning/datasources/prometheus.yml",
        "provisioning/datasources/loki.yml",
    },
    "prometheus": {"Dockerfile", "prometheus.yml"},
    "loki": {"Dockerfile", "config.yml"},
    "alloy": {"Dockerfile", "config.alloy"},
}
AUDIT_FILES = {"README.md", "REVIEW.md", "SECURITY.md", "review_binary.go"}
LABEL = "io.devops.infrastructure.source"


def fingerprint(name, root=ROOT, *, inputs=INPUTS):
    folder = root / name
    files = {
        p.relative_to(folder).as_posix()
        for p in folder.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    }
    if files - inputs[name] - AUDIT_FILES:
        raise ValueError(
            "New build context files require updating the infrastructure input list"
        )
    sources = {
        # Build inputs are text. Match Git's Linux checkout even with CRLF on Windows.
        n: hashlib.sha256((folder / n).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        for n in sorted(inputs[name])
    }
    sources["revision.txt"] = (root / "revision.txt").read_text().strip()
    return hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()


def execute(*args):
    # Build logs contain compiler failures and progress needed to diagnose CI.
    # Other Docker output can include configuration and stays captured.
    if args[0] == "build":
        subprocess.run(["docker", *args], check=True)
        return ""
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, encoding="utf-8"
    )
    if result.returncode:
        raise RuntimeError(f"Infrastructure Docker {args[0]} failed")
    return result.stdout.strip()


def registry_digest(reference):
    result = subprocess.run(
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            reference,
            "--format",
            "{{json .Manifest}}",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode:
        # Never treat an authentication, rate-limit or connection failure as a
        # cache miss and overwrite a previously published infrastructure tag.
        if reference in result.stderr and result.stderr.strip().endswith(": not found"):
            return None
        raise RuntimeError(
            "Infrastructure registry lookup failed; existing tags were not changed"
        )
    digest = json.loads(result.stdout).get("digest", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Registry returned an invalid infrastructure digest")
    return digest


def local_id(reference):
    value = execute("image", "inspect", reference, "--format", "{{.Id}}")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError("Invalid local infrastructure image ID")
    return value


def resolve(repository, *, root=ROOT, inputs=INPUTS):
    """Resolve already published recipes without pulling, building or scanning."""
    if not re.fullmatch(r"[a-z0-9_-]+/[a-z0-9_.-]+", repository):
        raise ValueError("Expected a Docker Hub namespace/repository")
    images = {}
    for name in inputs:
        source = fingerprint(name, root, inputs=inputs)
        tag = f"{repository}:infra-{name}-{source}"
        digest = registry_digest(tag)
        if not digest:
            raise RuntimeError(
                f"No published image for {name}. Run the Infrastructure workflow "
                "for this branch first; application CI never builds infrastructure."
            )
        images[name] = {
            "source": source,
            "tag": tag,
            "reference": repository + "@" + digest,
        }
    return {
        "repository": repository,
        "images": images,
        "scanned": False,
        "references_only": True,
    }


def prepare(repository, *, root=ROOT, inputs=INPUTS, prune_build_cache=False):
    if not re.fullmatch(r"[a-z0-9_-]+/[a-z0-9_.-]+", repository):
        raise ValueError("Expected a Docker Hub namespace/repository")
    images = {}
    for name in inputs:
        source = fingerprint(name, root, inputs=inputs)
        tag = f"{repository}:infra-{name}-{source}"
        digest = registry_digest(tag)
        if digest:
            reference = repository + "@" + digest
            execute("pull", reference)
            labels = (
                json.loads(
                    execute(
                        "image",
                        "inspect",
                        reference,
                        "--format",
                        "{{json .Config.Labels}}",
                    )
                )
                or {}
            )
            if labels.get(LABEL) != source:
                raise ValueError(
                    "Published infrastructure does not match its build-input label"
                )
        else:
            reference = tag
            execute(
                "build",
                "--platform",
                "linux/amd64",
                "--label",
                LABEL + "=" + source,
                "--tag",
                tag,
                str(root / name),
            )
        images[name] = {
            "source": source,
            "tag": tag,
            "reference": reference,
            "image_id": local_id(reference),
            "new": digest is None,
        }
        if digest is None and prune_build_cache:
            # Explicit CI-only option; keep loaded images and all data volumes.
            execute("builder", "prune", "--all", "--force")
    return {"repository": repository, "images": images, "scanned": False}


def validate(state, *, root=ROOT, inputs=INPUTS):
    if state.get("references_only"):
        raise ValueError("Resolved references are not a prepared scan or publication")
    if set(state["images"]) != set(inputs):
        raise ValueError("Incomplete infrastructure image set")
    for name, item in state["images"].items():
        source = fingerprint(name, root, inputs=inputs)
        if (
            item["source"] != source
            or item["tag"] != f"{state['repository']}:infra-{name}-{source}"
        ):
            raise ValueError("Infrastructure inputs changed during CI")
        if local_id(item["reference"]) != item["image_id"]:
            raise ValueError("Infrastructure image changed after preparation")


def scan_prepared(state, output, *, root=ROOT, inputs=INPUTS):
    state["scanned"] = False
    validate(state, root=root, inputs=inputs)
    successful = True
    for index, item in enumerate(state["images"].values()):
        successful = scan(item["reference"], output / str(index)) and successful
    validate(state, root=root, inputs=inputs)
    if not successful:
        raise RuntimeError("Infrastructure vulnerability scan blocked publication")
    state["scanned"] = True


def publish(state, *, root=ROOT, inputs=INPUTS):
    if not state.get("scanned"):
        raise ValueError(
            "All infrastructure images must pass scanning before publication"
        )
    validate(state, root=root, inputs=inputs)
    # A single CI job concurrency group serializes our publications. Still check
    # for an independently created tag; do not replace it even after a race.
    for item in state["images"].values():
        if item["new"] and registry_digest(item["tag"]) is not None:
            raise RuntimeError(
                "Infrastructure tag appeared during CI; rerun to reuse it"
            )
    for item in state["images"].values():
        if item["new"]:
            execute("push", item["tag"])
            digest = registry_digest(item["tag"])
            if not digest:
                raise RuntimeError("Published infrastructure manifest is missing")
            reference = state["repository"] + "@" + digest
            execute("pull", reference)
            if local_id(reference) != item["image_id"]:
                raise RuntimeError(
                    "Published infrastructure differs from the scanned image"
                )
            item["reference"] = reference
        print(item["reference"], flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["resolve", "prepare", "scan", "publish"])
    parser.add_argument("--repository")
    parser.add_argument(
        "--group", choices=["infrastructure", "monitoring"], default="infrastructure"
    )
    parser.add_argument(
        "--prune-build-cache",
        action="store_true",
        help="Disposable CI runners only: remove build cache after each newly built image",
    )
    parser.add_argument("--state", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
    )
    args = parser.parse_args()
    args.state = args.state or Path(f".local-history/{args.group}-images.json")
    args.output = args.output or Path(f".local-history/security-scans/{args.group}")
    options = (
        {"root": ROOT, "inputs": INPUTS}
        if args.group == "infrastructure"
        else {"root": ROOT.parent / "monitoring", "inputs": MONITORING_INPUTS}
    )
    if args.action == "resolve":
        state = resolve(args.repository or "", **options)
    elif args.action == "prepare":
        state = prepare(
            args.repository or "", prune_build_cache=args.prune_build_cache, **options
        )
    else:
        state = json.loads(args.state.read_text(encoding="utf-8"))
        if args.action == "scan":
            state["scanned"] = False
            args.state.write_text(json.dumps(state, indent=2), encoding="utf-8")
            scan_prepared(state, args.output, **options)
        else:
            publish(state, **options)
    args.state.parent.mkdir(parents=True, exist_ok=True)
    args.state.write_text(json.dumps(state, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
