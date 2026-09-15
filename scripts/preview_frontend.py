"""Serve local UI read-only in a separate loopback-only preview container."""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTAINER = "devops_project-frontend-1"
PREVIEW = "devops_project-frontend-preview"
LABEL = "devops.preview"


def inspect(name):
    result = subprocess.run(
        ["docker", "inspect", name], capture_output=True, text=True, encoding="utf-8"
    )
    return json.loads(result.stdout)[0] if result.returncode == 0 else None


def main():
    try:
        frontend = inspect(CONTAINER)
        if not frontend or not frontend["State"]["Running"]:
            raise RuntimeError("Start Docker Desktop and run make start first")
        network = next(iter(frontend["NetworkSettings"]["Networks"]))
        current = inspect(PREVIEW)
        if current:
            if current["Config"]["Labels"].get(LABEL) != "true":
                raise RuntimeError("Preview name is occupied by an unrelated container")
            subprocess.run(["docker", "rm", "-f", PREVIEW], check=True)
        config = ROOT / ".local-history/preview/nginx.conf"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            "pid /tmp/nginx.pid;\nerror_log /dev/null;\nevents {}\nhttp {\n"
            "include /etc/nginx/mime.types;\n"
            "client_body_temp_path /tmp/client;\nproxy_temp_path /tmp/proxy;\n"
            "fastcgi_temp_path /tmp/fastcgi;\nuwsgi_temp_path /tmp/uwsgi;\n"
            "scgi_temp_path /tmp/scgi;\n"
            "include /etc/nginx/security-preview.conf;\n}\n",
            encoding="utf-8",
        )
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                PREVIEW,
                "--label",
                LABEL + "=true",
                "--network",
                network,
                "--publish",
                "127.0.0.1:8081:8080",
                "--read-only",
                "--user",
                "101:101",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--memory=128m",
                "--cpus=0.5",
                "--pids-limit=64",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=32m,mode=1777",
                "--log-driver=json-file",
                "--log-opt=max-size=5m",
                "--log-opt=max-file=2",
                "--mount",
                f"type=bind,source={ROOT / 'frontend'},target=/usr/share/nginx/html,readonly",
                "--mount",
                f"type=bind,source={ROOT / 'frontend/nginx.conf'},target=/etc/nginx/security-preview.conf,readonly",
                "--mount",
                f"type=bind,source={config},target=/etc/nginx/preview.conf,readonly",
                "--entrypoint",
                "nginx",
                frontend["Image"],
                "-c",
                "/etc/nginx/preview.conf",
                "-g",
                "daemon off;",
            ],
            check=True,
        )
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Preview failed: {error}") from None
    print("Preview: http://localhost:8081 - refresh with Ctrl+F5 after saving files.")
    print("Preview uses the running application's API and database.")


if __name__ == "__main__":
    main()
