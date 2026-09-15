"""Provision an explicit SELECT-only role using an administrator connection."""

import json
import sys

from sqlalchemy import text

from src.db.database import get_engine

TABLES = (
    "studies",
    "predictions",
    "feedback",
    "model_versions",
    "training_runs",
    "dataset_rows",
)


def provision(credentials):
    if (
        credentials["POSTGRES_USER"] != "diabetes_metrics"
        or len(credentials["POSTGRES_PASSWORD"]) < 32
    ):
        raise ValueError("Unexpected metrics database identity")
    with get_engine().begin() as connection:
        connection.exec_driver_sql("SET LOCAL lock_timeout = '10s'")
        connection.exec_driver_sql("SELECT pg_advisory_xact_lock(20260911, 1)")
        row = connection.execute(
            text(
                "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname = 'diabetes_metrics'"
            )
        ).first()
        if row is None:
            connection.exec_driver_sql(
                "CREATE ROLE diabetes_metrics LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 4"
            )
        elif any(row):
            raise RuntimeError("Refusing to reuse an existing privileged role")
        memberships = connection.exec_driver_sql(
            "SELECT 1 FROM pg_auth_members WHERE member = (SELECT oid FROM pg_roles WHERE rolname = 'diabetes_metrics')"
        ).first()
        if memberships:
            raise RuntimeError("Metrics role has unexpected inherited permissions")
        ownership = connection.exec_driver_sql(
            "SELECT 1 FROM pg_shdepend WHERE refclassid = 'pg_authid'::regclass "
            "AND refobjid = (SELECT oid FROM pg_roles WHERE rolname = 'diabetes_metrics') "
            "AND deptype = 'o' LIMIT 1"
        ).first()
        if ownership:
            raise RuntimeError("Metrics role unexpectedly owns database objects")
        connection.exec_driver_sql("ALTER ROLE diabetes_metrics CONNECTION LIMIT 4")
        connection.exec_driver_sql(
            "ALTER ROLE diabetes_metrics PASSWORD %s",
            (credentials["POSTGRES_PASSWORD"],),
        )
        connection.exec_driver_sql(
            "ALTER ROLE diabetes_metrics SET default_transaction_read_only = on"
        )
        connection.exec_driver_sql(
            "ALTER ROLE diabetes_metrics SET statement_timeout = '5s'"
        )
        connection.exec_driver_sql(
            "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM diabetes_metrics"
        )
        connection.exec_driver_sql("GRANT USAGE ON SCHEMA public TO diabetes_metrics")
        for table in TABLES:
            connection.exec_driver_sql(
                f"GRANT SELECT ON TABLE public.{table} TO diabetes_metrics"
            )


if __name__ == "__main__":
    try:
        provision(json.load(sys.stdin))
    except Exception:
        raise SystemExit("Metrics database role provisioning failed") from None
