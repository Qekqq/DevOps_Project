"""Launch a disposable maintenance process with private stdin."""

import json
import subprocess


def run_maintenance(compose, env, database, command, *, extra=None):
    result = subprocess.run(
        [
            *compose,
            "run",
            "--rm",
            "--no-deps",
            "--pull",
            "never",
            "-T",
            "diabetes-api",
            "python",
            "-m",
            "scripts.database_maintenance",
            *command,
        ],
        env=env,
        input=json.dumps({"database": database, **(extra or {})}),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode:
        raise RuntimeError(
            "Database maintenance failed; private diagnostics suppressed"
        )


def installed_admin_credentials(services):
    values = dict(
        item.split("=", 1) for item in services["db"]["Config"]["Env"] if "=" in item
    )
    return {
        "POSTGRES_HOST": "db",
        "POSTGRES_PORT": "5432",
        **{
            key: values[key]
            for key in ("POSTGRES_USER", "POSTGRES_DB", "POSTGRES_PASSWORD")
        },
    }
