"""One-shot administrator commands: credentials enter via stdin, never CLI/env."""

import json
import os
import runpy
import sys

from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from src.db import database

COMMANDS = {
    "scripts.update_database",
    "src.register_release",
    "scripts.activate_model_release",
}


def execute(payload, command):
    if not command or command[0] not in COMMANDS | {
        "provision-runtime",
        "integration-check",
        "create-user",
    }:
        raise ValueError("Unsupported maintenance command")
    if command[0] == "integration-check" and os.getenv("RUN_INTEGRATION_TESTS") != "1":
        raise ValueError("Integration checks require a disposable test environment")
    if database._engine is not None:
        raise RuntimeError("Maintenance requires a fresh process")
    engine = create_engine(
        database.build_database_url(payload["database"]),
        hide_parameters=True,
        poolclass=NullPool,
    )
    database._engine = engine
    try:
        if command[0] == "provision-runtime":
            from scripts.provision_metrics_user import provision as metrics
            from scripts.provision_runtime_users import provision

            provision(payload["identities"])
            metrics(payload["metrics"])
        elif command[0] == "create-user":
            from scripts.create_user import create_record

            create_record(payload["user"])
        elif command[0] == "integration-check":
            sys.argv = [
                "tests/run_integration.py",
                "tests/integration/test_database_contract.py",
                "tests/integration/test_prediction_flow.py",
                "tests/integration/test_monitoring_stack.py",
                "tests/integration/test_ml_report.py",
                "tests/integration/test_runtime_security.py",
                "-v",
                "-p",
                "no:cacheprovider",
            ]
            runpy.run_path("tests/run_integration.py", run_name="__main__")
        else:
            sys.argv = command
            runpy.run_module(command[0], run_name="__main__")
    finally:
        engine.dispose()
        database._engine = None
        database._session_factory = None


if __name__ == "__main__":
    try:
        execute(json.load(sys.stdin), sys.argv[1:])
    except Exception:
        raise SystemExit(
            "Database maintenance failed; private diagnostics suppressed"
        ) from None
