"""Пакет выкладки: точные образы и конфигурация из проверяемого коммита."""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

from scripts.infrastructure_release import apply_images
from scripts.model_delivery import inventory

ROOT = Path(__file__).resolve().parents[1]


def package_release(
    output,
    commit,
    run_id,
    api_image,
    frontend_image,
    infrastructure_images=None,
    grafana_image=None,
    monitoring_images=None,
):
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Ожидается полный SHA коммита")
    if monitoring_images is not None and set(monitoring_images) != {
        "prometheus",
        "loki",
        "alloy",
    }:
        raise ValueError("All three monitoring images are required")
    for image in (
        api_image,
        frontend_image,
        *(infrastructure_images or {}).values(),
        *(monitoring_images or {}).values(),
        *([grafana_image] if grafana_image else []),
    ):
        if not re.fullmatch(r"[a-z0-9./_-]+@sha256:[0-9a-f]{64}", image):
            raise ValueError("Образы приложения должны быть указаны по digest")
    config = json.loads(
        subprocess.check_output(
            [
                "docker",
                "compose",
                "-f",
                str(ROOT / "docker-compose.yml"),
                "config",
                "--no-interpolate",
                "--no-path-resolution",
                "--format",
                "json",
            ],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
        )
    )
    if infrastructure_images is not None:
        apply_images(config, infrastructure_images)
    for name, service in config["services"].items():
        service.pop("build", None)
        if name in (
            "diabetes-api",
            "kafka-consumer",
            "metrics-exporter",
            "docker-stats",
        ):
            service["image"] = api_image
        elif name == "frontend":
            service["image"] = frontend_image
        elif name == "grafana":
            if not grafana_image:
                raise ValueError(
                    "Укажите проверенный образ Grafana с источником метрик"
                )
            service["image"] = grafana_image
        elif name in {"prometheus", "loki", "alloy"}:
            if not monitoring_images:
                raise ValueError("Provide scanned monitoring images")
            service["image"] = monitoring_images[name]
        else:
            subprocess.run(["docker", "pull", service["image"]], check=True)
            if "@sha256:" in service["image"]:
                # Preserve the digest explicitly reviewed in Compose, even if
                # Docker knows other repository digests for this same image.
                continue
            digests = json.loads(
                subprocess.check_output(
                    [
                        "docker",
                        "image",
                        "inspect",
                        service["image"],
                        "--format",
                        "{{json .RepoDigests}}",
                    ],
                    text=True,
                )
            )
            service["image"] = digests[0]
    output.mkdir(parents=True, exist_ok=False)
    for directory in ("monitoring", "vault", "db"):
        shutil.copytree(ROOT / directory, output / directory)
    (output / "docker-compose.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    hashes = {
        path.relative_to(output).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in output.rglob("*")
        if path.is_file()
    }
    manifest = {"commit": commit, "ci_run_id": str(run_id), "files": hashes}
    if infrastructure_images is not None:
        manifest["infrastructure_generation"] = 2
    # Hashes may be public; model bytes must stay in the authenticated DVC store.
    manifest["model_files"] = inventory(ROOT / "models")
    (output / "release.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--api-image", required=True)
    parser.add_argument("--frontend-image", required=True)
    parser.add_argument("--grafana-image", required=True)
    parser.add_argument("--prometheus-image", required=True)
    parser.add_argument("--loki-image", required=True)
    parser.add_argument("--alloy-image", required=True)
    parser.add_argument("--postgres-image", required=True)
    parser.add_argument("--vault-image", required=True)
    parser.add_argument("--kafka-image", required=True)
    args = parser.parse_args()
    package_release(
        args.output,
        args.commit,
        args.run_id,
        args.api_image,
        args.frontend_image,
        {
            "db": args.postgres_image,
            "vault": args.vault_image,
            "kafka": args.kafka_image,
        },
        grafana_image=args.grafana_image,
        monitoring_images={
            "prometheus": args.prometheus_image,
            "loki": args.loki_image,
            "alloy": args.alloy_image,
        },
    )
