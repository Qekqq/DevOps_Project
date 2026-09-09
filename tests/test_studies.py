from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import studies
from src.auth import current_user
from src.db.database import get_db


@pytest.fixture
def setup(monkeypatch):
    app = FastAPI()
    app.include_router(studies.router)
    user = SimpleNamespace(id=1, role="user")
    db = MagicMock()
    app.dependency_overrides[current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    study = SimpleNamespace(
        id=3,
        created_by=1,
        patient_code="PAT001",
        study_date=date(2026, 9, 8),
        features={"glucose": 148},
    )
    monkeypatch.setattr(studies, "get_study", lambda *args: study)
    with TestClient(app) as client:
        yield client, user, study, db


def test_user_sees_own_models_but_not_feedback(setup):
    client, _, _, db = setup
    champion = SimpleNamespace(
        id=10, model_name="LR", model_version="lr1", role="champion"
    )
    shadow = SimpleNamespace(
        id=11, model_name="DT", model_version="dt1", role="challenger"
    )
    saved = SimpleNamespace(
        model_version=champion,
        model_version_id=10,
        role_at_prediction="champion",
        prediction=1,
        probability=0.79,
    )
    db.scalars.return_value.all.side_effect = [[saved], [champion, shadow]]
    response = client.get("/studies/PAT001/2026-09-08")
    assert response.status_code == 200
    data = response.json()
    assert data["features"]["glucose"] == 148
    assert data["can_feedback"] is False
    assert data["feedback"] is None
    assert [p["status"] for p in data["predictions"]] == ["ready", "pending"]
    assert data["predictions"][1]["probability"] is None
    db.scalar.assert_not_called()


def test_user_cannot_read_another_users_study(setup):
    client, _, study, _ = setup
    study.created_by = 2
    assert client.get("/studies/PAT001/2026-09-08").status_code == 404


def test_user_cannot_save_feedback(setup):
    client, _, _, db = setup
    assert (
        client.put(
            "/studies/PAT001/2026-09-08/feedback", json={"true_label": 1}
        ).status_code
        == 403
    )
    db.commit.assert_not_called()


def test_history_and_snapshot_are_admin_only(setup):
    client, _, _, _ = setup
    assert client.get("/studies").status_code == 403
    assert client.post("/studies/snapshot").status_code == 403


def test_history_returns_database_rows(setup):
    client, user, study, db = setup
    user.role = "admin"
    db.scalar.side_effect = [1, 1]
    model = SimpleNamespace(id=1, role="champion", model_name="LR", model_version="lr1")
    db.scalars.return_value.all.return_value = [model]
    db.execute.return_value.all.return_value = [(study, model, None, 0)]
    response = client.get("/studies?patient=PAT&feedback=filled&page=2")
    assert response.status_code == 200
    data = response.json()
    assert data["page"] == 1
    assert data["total"] == 1
    assert data["items"][0]["patient"] == study.patient_code
    assert data["items"][0]["feedback"] == 0
    assert data["items"][0]["model"]["prediction"] is None


def test_history_rejects_reversed_period(setup):
    client, user, _, db = setup
    user.role = "admin"
    response = client.get("/studies?date_from=2026-09-08&date_to=2026-09-01")
    assert response.status_code == 422
    db.scalar.assert_not_called()


def test_snapshot_uses_dates_and_registers_dataset(setup, monkeypatch, tmp_path):
    import pandas as pd

    client, user, _, db = setup
    user.role = "admin"
    frame = pd.DataFrame({"outcome": [0]})
    read = MagicMock(return_value=frame)
    path = tmp_path / "data.csv"
    path.write_bytes(b"outcome\n0\n")
    save = MagicMock(return_value=path)
    register = MagicMock()
    monkeypatch.setattr(studies, "read_confirmed_studies", read)
    monkeypatch.setattr(studies, "save_snapshot", save)
    monkeypatch.setattr(studies, "import_raw_dataset", register)
    response = client.post(
        "/studies/snapshot?date_from=2026-09-01&date_to=2026-09-08",
        json={"name": "  Сентябрь 2026  "},
    )
    assert response.status_code == 200
    read.assert_called_once_with(
        db, date_from=date(2026, 9, 1), date_to=date(2026, 9, 8)
    )
    register.assert_called_once_with(db, path, name="Сентябрь 2026")
    assert save.call_args.kwargs["name"] == "Сентябрь 2026"
    from urllib.parse import unquote

    assert "snapshot-Сентябрь 2026.csv" in unquote(
        response.headers["content-disposition"]
    )
    assert response.text == "outcome\n0\n"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 101},
        {"name": "../abc"},
        {"name": "a\nb"},
        {"name": 123},
    ],
)
def test_snapshot_rejects_invalid_name_without_writing(setup, monkeypatch, body):
    client, user, _, db = setup
    user.role = "admin"
    save = MagicMock()
    monkeypatch.setattr(studies, "save_snapshot", save)
    assert client.post("/studies/snapshot", json=body).status_code == 422
    save.assert_not_called()
    db.commit.assert_not_called()


def test_admin_saves_feedback_with_actor(setup, monkeypatch):
    client, user, _, db = setup
    user.role = "admin"
    save = MagicMock(return_value=SimpleNamespace(true_label=0))
    monkeypatch.setattr(studies, "save_prediction_feedback", save)
    response = client.put("/studies/PAT001/2026-09-08/feedback", json={"true_label": 0})
    assert response.status_code == 200
    save.assert_called_once_with(db, study_id=3, true_label=0, created_by_user_id=1)
    db.commit.assert_called_once()


@pytest.mark.parametrize("value", [2, -1, True, "1", 0.5])
def test_feedback_rejects_invalid_labels(setup, value):
    client, user, _, db = setup
    user.role = "admin"
    assert (
        client.put(
            "/studies/PAT001/2026-09-08/feedback", json={"true_label": value}
        ).status_code
        == 422
    )
    db.commit.assert_not_called()
