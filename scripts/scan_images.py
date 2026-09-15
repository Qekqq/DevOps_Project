"""Scan immutable images without exposing the Docker socket to the scanner."""

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from scripts.vulnerability_review import assess, blocks

SCANNER = "aquasec/trivy@sha256:62b1e65e8869bc4b4c6aa4fa2b21595256c7c2f6018a9d9ad61caf87187c1969"
CACHE = Path(__file__).resolve().parents[1] / ".local-history/security-scans/cache"
PUBLIC_IMAGES = {
    "hashicorp/vault",
    "postgres",
    "bitnamilegacy/kafka",
    "apache/kafka",
    "grafana/grafana",
    "grafana/alloy",
    "grafana/loki",
    "prom/prometheus",
}


def scan(image, directory):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / "image.tar"
    report = directory / "report.json"
    # All image groups share the database; Trivy still checks for updates.
    cache = CACHE
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "tmp").mkdir(exist_ok=True)
    status_path = directory / "scan-status.json"
    status = {
        "image": image,
        "scanner": SCANNER,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "state": "started",
    }
    status_path.write_text(json.dumps(status), encoding="utf-8")
    # A failed retry must never reuse the previous successful report.
    report.unlink(missing_ok=True)
    try:
        # Export the resolved local platform image, not a multi-platform index
        # whose other blobs may not be present in Docker Desktop's image store.
        image_id = subprocess.check_output(
            ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
            text=True,
        ).strip()
        repository = image.split("@")[0].split(":")[0]
        remote = repository in PUBLIC_IMAGES and "@sha256:" in image
        if not remote:
            subprocess.run(
                ["docker", "image", "save", "--output", str(archive), image_id],
                check=True,
            )
        # Public infrastructure is fetched by immutable digest. This also avoids
        # incomplete multi-platform tar exports in Docker Desktop's containerd store.
        # Private application images never require registry credentials in Trivy.
        source = (
            ["--image-src", "remote", "--platform", "linux/amd64", image]
            if remote
            else ["--input", "/work/image.tar"]
        )
        # Linux bind mounts keep the runner's ownership. With all capabilities
        # dropped, even container root cannot write to another user's 0755 folder.
        # Docker Desktop handles Windows bind-mount ownership itself.
        user = ["--user", f"{os.getuid()}:{os.getgid()}"] if os.name != "nt" else []
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                *user,
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--memory=1g",
                "--cpus=1",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=512m",
                "--env",
                "TMPDIR=/cache/tmp",
                "--mount",
                f"type=bind,source={directory},target=/work",
                "--mount",
                f"type=bind,source={cache},target=/cache",
                SCANNER,
                "image",
                "--no-progress",
                "--cache-dir",
                "/cache",
                "--scanners",
                "vuln",
                "--format",
                "json",
                "--output",
                "/work/report.json",
                *source,
            ],
            check=True,
        )
        result = json.loads(report.read_text(encoding="utf-8"))
        result["RequestedImage"] = image
        result["ResolvedImageId"] = image_id
        reviews = assess(result, image_id)
        result["ApplicabilityReviews"] = reviews
        report.write_text(json.dumps(result, indent=2), encoding="utf-8")
        vulnerabilities = [
            v for r in result.get("Results", []) for v in r.get("Vulnerabilities", [])
        ]
        blocking = [
            v
            for r in result.get("Results", [])
            for v in r.get("Vulnerabilities", [])
            if blocks(r, v, reviews)
        ]
        print(
            f"{image}: {len(vulnerabilities)} findings, {len(blocking)} blocking HIGH/CRITICAL, {len(reviews)} reviewed not affected",
            flush=True,
        )
        for item in blocking:
            print(
                f"  {item['VulnerabilityID']} {item['PkgName']} {item['InstalledVersion']} -> {item['FixedVersion']}"
            )
        status.update(
            state="complete",
            completed_at=datetime.now(timezone.utc).isoformat(),
            image_id=image_id,
            blocking=len(blocking),
        )
        return not blocking
    except Exception as error:
        # Keep credentials / command lines out of the portable report.
        status.update(state="failed", error=type(error).__name__)
        raise
    finally:
        status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")
        # Only the exact tar created by this invocation; reports are retained.
        archive.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("images", nargs="*")
    parser.add_argument("--compose", type=Path)
    parser.add_argument(
        "--output", type=Path, default=Path(".local-history/security-scans")
    )
    args = parser.parse_args()
    images = args.images
    if args.compose:
        config = json.loads(
            subprocess.check_output(
                [
                    "docker",
                    "compose",
                    "-f",
                    str(args.compose),
                    "config",
                    "--format",
                    "json",
                ],
                encoding="utf-8",
            )
        )
        images += [s["image"] for s in config["services"].values() if s.get("image")]
    if not images:
        parser.error("Provide images or --compose")
    successful = True
    for index, image in enumerate(dict.fromkeys(images)):
        successful = scan(image, args.output / str(index)) and successful
    if not successful:
        raise SystemExit("Vulnerable images: release blocked; see scan reports")


if __name__ == "__main__":
    main()
