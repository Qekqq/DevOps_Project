"""Пакет выкладки: точные образы и конфигурация из проверяемого коммита."""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def package_release(output, commit, run_id, api_image, frontend_image):
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Ожидается полный SHA коммита")
    for image in (api_image, frontend_image):
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
    for name, service in config["services"].items():
        service.pop("build", None)
        if name in ("diabetes-api", "kafka-consumer", "metrics-exporter"):
            service["image"] = api_image
        elif name == "frontend":
            service["image"] = frontend_image
        else:
            subprocess.run(["docker", "pull", service["image"]], check=True)
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
    args = parser.parse_args()
    package_release(
        args.output, args.commit, args.run_id, args.api_image, args.frontend_image
    )
