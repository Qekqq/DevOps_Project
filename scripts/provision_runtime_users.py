"""Explicit runtime grants; schema ownership stays with the maintenance account."""

from sqlalchemy import text

from src.db.database import get_engine

READ_TABLES = (
    "users",
    "user_sessions",
    "studies",
    "study_edits",
    "datasets",
    "dataset_rows",
    "training_runs",
    "model_versions",
    "predictions",
    "shadow_retries",
    "feedback",
    "feedback_history",
    "model_role_history",
)
GRANTS = {
    "diabetes_api": {
        "SELECT": READ_TABLES,
        "INSERT": (
            "user_sessions",
            "studies",
            "study_edits",
            "predictions",
            "feedback",
            "datasets",
            "dataset_rows",
        ),
        "UPDATE": ("studies", "predictions", "feedback"),
        "DELETE": ("user_sessions",),
    },
    "diabetes_consumer": {
        "SELECT": ("studies", "predictions", "model_versions", "shadow_retries"),
        "INSERT": ("studies", "predictions", "shadow_retries"),
        "UPDATE": ("shadow_retries",),
        "DELETE": ("shadow_retries",),
    },
}


def provision(identities):
    if set(identities) != set(GRANTS) or any(
        not isinstance(value, str) or len(value) < 32 for value in identities.values()
    ):
        raise ValueError("Unexpected runtime database identities")
    with get_engine().begin() as conn:
        conn.exec_driver_sql("SET LOCAL lock_timeout = '10s'")
        conn.exec_driver_sql("SELECT pg_advisory_xact_lock(20260911, 1)")
        # PUBLIC privileges are inherited even after revoking a role's direct grants.
        db = conn.exec_driver_sql("SELECT current_database()").scalar_one()
        quoted_db = conn.dialect.identifier_preparer.quote_identifier(db)
        conn.exec_driver_sql(
            f"REVOKE CREATE, TEMPORARY ON DATABASE {quoted_db} FROM PUBLIC"
        )
        conn.exec_driver_sql("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        conn.exec_driver_sql("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC")
        conn.exec_driver_sql("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC")
        # Audit rows can only be appended by the trusted trigger, not by clients.
        for function in ("audit_feedback", "audit_model_role"):
            conn.exec_driver_sql(f"ALTER FUNCTION public.{function}() SECURITY DEFINER")
            conn.exec_driver_sql(
                f"ALTER FUNCTION public.{function}() SET search_path = pg_catalog, public, pg_temp"
            )
        for role, grants in GRANTS.items():
            row = conn.execute(
                text(
                    "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
                    "FROM pg_roles WHERE rolname = :role"
                ),
                {"role": role},
            ).first()
            if row is None:
                conn.exec_driver_sql(
                    f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                )
            elif any(row):
                raise RuntimeError("Refusing to reuse privileged runtime role")
            for query in (
                "SELECT 1 FROM pg_auth_members WHERE member = (SELECT oid FROM pg_roles WHERE rolname = :role)",
                "SELECT 1 FROM pg_shdepend WHERE refclassid = 'pg_authid'::regclass AND refobjid = (SELECT oid FROM pg_roles WHERE rolname = :role) AND deptype = 'o'",
            ):
                if conn.execute(text(query), {"role": role}).first():
                    raise RuntimeError(
                        "Runtime role has inherited privileges or owns objects"
                    )
            conn.exec_driver_sql(f"ALTER ROLE {role} LOGIN CONNECTION LIMIT 12")
            conn.exec_driver_sql(f"ALTER ROLE {role} PASSWORD %s", (identities[role],))
            conn.exec_driver_sql(f"ALTER ROLE {role} SET statement_timeout = '30s'")
            conn.exec_driver_sql(
                f"ALTER ROLE {role} SET idle_in_transaction_session_timeout = '60s'"
            )
            conn.exec_driver_sql(f"REVOKE ALL ON DATABASE {quoted_db} FROM {role}")
            conn.exec_driver_sql(f"GRANT CONNECT ON DATABASE {quoted_db} TO {role}")
            conn.exec_driver_sql(f"REVOKE ALL ON SCHEMA public FROM {role}")
            conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {role}")
            conn.exec_driver_sql(
                f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role}"
            )
            conn.exec_driver_sql(
                f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {role}"
            )
            # Table-level REVOKE does not clear old column-level grants.
            for table in READ_TABLES:
                columns = (
                    conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=:table"
                        ),
                        {"table": table},
                    )
                    .scalars()
                    .all()
                )
                names = ",".join(
                    conn.dialect.identifier_preparer.quote_identifier(c)
                    for c in columns
                )
                conn.exec_driver_sql(
                    f"REVOKE SELECT ({names}), INSERT ({names}), UPDATE ({names}), REFERENCES ({names}) ON public.{table} FROM {role}"
                )
            for privilege, tables in grants.items():
                for table in tables:
                    conn.exec_driver_sql(
                        f"GRANT {privilege} ON public.{table} TO {role}"
                    )
            if role == "diabetes_api":
                conn.exec_driver_sql(
                    "GRANT UPDATE (role) ON public.model_versions TO diabetes_api"
                )
            for table in grants["INSERT"]:
                sequence = (
                    conn.execute(
                        text("SELECT pg_get_serial_sequence(:table, 'id')"),
                        {"table": table},
                    ).scalar()
                    if table not in ("user_sessions", "shadow_retries", "dataset_rows")
                    else None
                )
                if sequence:
                    # pg_get_serial_sequence returns a server-quoted qualified name.
                    conn.exec_driver_sql(
                        f"GRANT USAGE ON SEQUENCE {sequence} TO {role}"
                    )
