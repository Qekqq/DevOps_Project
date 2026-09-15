"""Actual SQL boundaries for runtime accounts on the disposable stack."""

import json
import os
import subprocess

import hvac
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from src.db.database import build_database_url


@pytest.fixture
def role_engines():
    if os.getenv("TEST_RESTRICTED_DATABASE") != "1":
        pytest.skip("Requires the isolated restricted-role stack")
    compose = json.loads(os.environ["TEST_COMPOSE_COMMAND"])
    project = compose[compose.index("-p") + 1]
    assert project != "devops_project"
    info = json.loads(
        subprocess.check_output(["docker", "inspect", f"{project}-db-1"], text=True)
    )[0]
    binding = info["HostConfig"]["PortBindings"]["5432/tcp"][0]
    assert binding["HostIp"] == "127.0.0.1"
    vault = hvac.Client(
        url=os.environ["TEST_VAULT_URL"],
        token=os.environ["TEST_VAULT_TOKEN"],
        verify=os.getenv("TEST_VAULT_CACERT") or True,
    )
    engines = {}
    try:
        for role in ("api", "consumer", "metrics"):
            credentials = vault.secrets.kv.v2.read_secret_version(
                path=f"database/{role}", raise_on_deleted_version=True
            )["data"]["data"]
            credentials.update(
                POSTGRES_HOST="127.0.0.1", POSTGRES_PORT=binding["HostPort"]
            )
            engines[role] = create_engine(
                build_database_url(credentials),
                hide_parameters=True,
                poolclass=NullPool,
            )
        yield engines
    finally:
        for engine in engines.values():
            engine.dispose()


def denied(engine, statement):
    with engine.connect() as conn:
        conn.exec_driver_sql("SET TRANSACTION READ WRITE")
        with pytest.raises(DBAPIError) as failure:
            conn.exec_driver_sql(statement)
        assert failure.value.orig.pgcode == "42501"
        conn.rollback()


def test_runtime_accounts_cannot_administer_database(role_engines):
    for role, engine in role_engines.items():
        with engine.connect() as conn:
            assert (
                conn.exec_driver_sql("SELECT current_user").scalar_one()
                == f"diabetes_{role}"
            )
            assert not any(
                conn.exec_driver_sql(
                    "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname=current_user"
                ).one()
            )
            conn.exec_driver_sql("SELECT id FROM model_versions LIMIT 1")
        for statement in (
            "CREATE TABLE public.security_denied(id int)",
            "CREATE TEMP TABLE security_denied(id int)",
            "ALTER TABLE studies DISABLE TRIGGER ALL",
            "UPDATE users SET role='admin' WHERE false",
            "INSERT INTO users (username, password_hash) VALUES ('security_denied', 'invalid')",
            "DELETE FROM studies WHERE false",
            "INSERT INTO feedback_history(study_id, new_label) VALUES (0, 1)",
        ):
            denied(engine, statement)
    for statement in (
        "SELECT * FROM users",
        "SELECT * FROM user_sessions",
        "UPDATE studies SET glucose=100 WHERE false",
        "UPDATE model_versions SET role='champion' WHERE false",
        "SELECT * FROM feedback",
    ):
        denied(role_engines["consumer"], statement)


def test_api_feedback_creates_audit_without_direct_history_write(role_engines):
    with role_engines["api"].connect() as conn:
        study = conn.exec_driver_sql(
            "INSERT INTO studies(patient_code,study_date,pregnancies,glucose,blood_pressure,skin_thickness,insulin,bmi,diabetes_pedigree_function,age) VALUES ('SEC901','2026-09-11',0,100,70,20,0,25,0.5,40) RETURNING id"
        ).scalar_one()
        conn.execute(
            text("INSERT INTO feedback(study_id,true_label) VALUES (:id,0)"),
            {"id": study},
        )
        conn.execute(
            text("UPDATE feedback SET true_label=1 WHERE study_id=:id"), {"id": study}
        )
        assert (
            conn.execute(
                text("SELECT count(*) FROM feedback_history WHERE study_id=:id"),
                {"id": study},
            ).scalar_one()
            == 2
        )
        conn.rollback()


def test_api_can_register_feedback_snapshot_but_not_rewrite_datasets(role_engines):
    with role_engines["api"].connect() as conn:
        dataset_id = conn.execute(
            text(
                "INSERT INTO datasets(dataset_name,dataset_version,source_type,source_path,source_sha256,row_count,selection_filters) "
                "VALUES ('snapshot privilege test',:version,'feedback','/app/data/feedback/test/data.csv',:sha,1,'{}') RETURNING id"
            ),
            {"version": "b" * 64, "sha": "a" * 64},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO dataset_rows(dataset_id,row_number,features,outcome) VALUES (:id,0,'{}',1)"
            ),
            {"id": dataset_id},
        )
        conn.rollback()
    for table in ("datasets", "dataset_rows"):
        denied(role_engines["api"], f"DELETE FROM {table} WHERE false")
        denied(
            role_engines["api"],
            f"UPDATE {table} SET {'dataset_name=dataset_name' if table == 'datasets' else 'outcome=outcome'} WHERE false",
        )


def test_consumer_can_save_predictions_and_retry_tasks(role_engines):
    with role_engines["consumer"].connect() as conn:
        model = conn.exec_driver_sql(
            "SELECT id FROM model_versions WHERE role='champion'"
        ).scalar_one()
        study = conn.exec_driver_sql(
            "INSERT INTO studies(patient_code,study_date,pregnancies,glucose,blood_pressure,skin_thickness,insulin,bmi,diabetes_pedigree_function,age) VALUES ('SEC902','2026-09-11',0,100,70,20,0,25,0.5,40) RETURNING id"
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO predictions(study_id,model_version_id,prediction,probability,role_at_prediction) VALUES (:id,:model,0,0.2,'champion')"
            ),
            {"id": study, "model": model},
        )
        conn.execute(
            text(
                "INSERT INTO shadow_retries(study_id,model_version_id) VALUES (:id,:model)"
            ),
            {"id": study, "model": model},
        )
        conn.execute(
            text("SELECT * FROM shadow_retries WHERE study_id=:id FOR UPDATE"),
            {"id": study},
        )
        conn.execute(
            text("UPDATE shadow_retries SET attempts=1 WHERE study_id=:id"),
            {"id": study},
        )
        conn.execute(
            text("DELETE FROM shadow_retries WHERE study_id=:id"), {"id": study}
        )
        conn.rollback()


def test_runtime_vault_policies_deny_administrator_and_other_service_secrets():
    if os.getenv("TEST_RESTRICTED_DATABASE") != "1":
        pytest.skip("Requires restricted roles")
    for prefix in ("API", "CONSUMER", "EXPORTER"):
        client = hvac.Client(
            url=os.environ["TEST_VAULT_URL"],
            verify=os.getenv("TEST_VAULT_CACERT") or True,
        )
        client.auth.approle.login(
            role_id=os.environ[f"{prefix}_VAULT_ROLE_ID"],
            secret_id=os.environ[f"{prefix}_VAULT_SECRET_ID"],
        )
        own = {"API": "api", "CONSUMER": "consumer", "EXPORTER": "metrics"}[prefix]
        for name in ("postgres", "api", "consumer", "metrics"):
            capability = client.sys.get_capabilities(
                paths=[f"secret/data/database/{name}"]
            )["data"]["capabilities"]
            assert capability == (["read"] if name == own else ["deny"])
        client.auth.token.revoke_self()
