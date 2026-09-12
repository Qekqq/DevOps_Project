"""Real PostgreSQL cohorts, current corrections and missing ground truth."""

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select, text

from src.db.database import get_session_factory
from src.db.models import ModelVersion, PredictionFeedback, PredictionHistory, Study
from src.ml_monitoring import read_report

pytestmark = pytest.mark.integration


def test_administrator_maintenance_creates_a_user_and_rejects_duplicates():
    from uuid import uuid4

    from sqlalchemy import delete

    from scripts.create_user import create_record
    from src.db.models import User
    from src.passwords import hash_password, verify_password

    name = "integration_" + uuid4().hex
    password = "test-only-disposable-password"
    record = {
        "username": name,
        "role": "user",
        "password_hash": hash_password(password),
    }
    try:
        create_record(record)
        with get_session_factory()() as db:
            user = db.scalar(select(User).where(User.username == name))
            assert user.role == "user" and user.is_active
            assert verify_password(password, user.password_hash)
        with pytest.raises(ValueError, match="already exists"):
            create_record(record)
    finally:
        with get_session_factory()() as db:
            db.execute(delete(User).where(User.username == name))
            db.commit()


def test_report_shared_axis_latest_corrections_and_late_labels():
    with get_session_factory()() as db:
        try:
            model = db.scalar(
                select(ModelVersion).where(ModelVersion.role == "challenger")
            )
            assert model is not None
            predictions, studies = [], []
            for index in range(3):
                study = Study(
                    patient_code=f"MLT{index:03d}",
                    study_date=date(1998, 3, 1 + index),
                    features=dict(
                        pregnancies=0,
                        glucose=0,
                        blood_pressure=70,
                        skin_thickness=20,
                        insulin=0,
                        bmi=30,
                        diabetes_pedigree_function=0.5,
                        age=40,
                    ),
                )
                db.add(study)
                db.flush()
                prediction = PredictionHistory(
                    study_id=study.id,
                    model_version_id=model.id,
                    role_at_prediction="challenger",
                    prediction=1 if index == 0 else 0,
                    probability=0.9 if index == 0 else 0.1,
                    created_at=datetime(2004, 7, 1 + index, tzinfo=timezone.utc),
                )
                db.add(prediction)
                if index == 0:
                    db.add(PredictionFeedback(study_id=study.id, true_label=1))
                predictions.append(prediction)
                studies.append(study)
            db.flush()
            filters = dict(
                model_version=model.model_version,
                date_from=date(2004, 7, 1),
                date_to=date(2004, 7, 2),
                resolution="day",
            )
            report = read_report(db, **filters)
            assert [p["samples"] for p in report["points"]] == [1, 1]
            assert report["summary"]["evaluated"] == 1
            assert report["summary"]["missing"]["glucose"] == 2
            assert report["points"][1]["metrics"]["recall"] is None
            # Ground truth arrives now, belongs to the old prediction interval.
            db.add(PredictionFeedback(study_id=studies[1].id, true_label=1))
            db.flush()
            updated = read_report(db, **filters)
            assert updated["summary"]["metrics"]["recall"] == 0.5
            assert updated["points"][1]["metrics"]["recall"] == 0
            # Corrections use the latest result, keeping the cohort's time unchanged.
            db.execute(text("SET LOCAL app.recalculate_study = 'on'"))
            predictions[1].prediction = 1
            predictions[1].probability = 0.95
            studies[1].glucose = 120
            db.flush()
            corrected = read_report(db, **filters)
            assert corrected["summary"]["metrics"]["recall"] == 1
            assert corrected["summary"]["missing"]["glucose"] == 1
            assert corrected["points"][1]["start"] == date(2004, 7, 2)
            study_axis = read_report(
                db,
                model_version=model.model_version,
                time_axis="study_date",
                resolution="day",
                date_from=date(1998, 3, 1),
                date_to=date(1998, 3, 2),
            )
            assert study_axis["summary"]["metrics"] == corrected["summary"]["metrics"]
            assert study_axis["summary"]["missing"] == corrected["summary"]["missing"]
            assert [p["samples"] for p in study_axis["points"]] == [1, 1]
            model.role = "archived"
            db.flush()
            archived = read_report(db, **filters)
            assert archived["summary"] == corrected["summary"]
            assert any(
                m["version"] == model.model_version and m["role"] == "archived"
                for m in archived["available_models"]
            )
        finally:
            db.rollback()
