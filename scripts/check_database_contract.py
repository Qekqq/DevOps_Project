"""Проверка ограничений и истории PostgreSQL; тестовые изменения откатываются."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from src.db.database import get_session_factory
from src.db.load_raw_dataset import import_raw_dataset
from src.db.models import FeedbackHistory, ModelRoleHistory, Study
from src.db.repositories import save_prediction_feedback
from src.feedback_dataset import read_confirmed_studies, save_snapshot


def main():
    with get_session_factory()() as db, TemporaryDirectory() as folder:
        try:
            assert (
                db.scalar(
                    text(
                        "SELECT count(*) FROM dataset_rows WHERE source_study_id IS NULL"
                    )
                )
                == 768
            )
            assert db.scalar(select(ModelRoleHistory.id).limit(1)) is not None
            values = dict(
                pregnancies=0,
                glucose=120,
                blood_pressure=70,
                skin_thickness=20,
                insulin=0,
                bmi=30,
                diabetes_pedigree_function=0.5,
                age=40,
            )
            study = Study(
                patient_code="AUD001", study_date=date(2026, 9, 1), features=values
            )
            db.add(study)
            db.flush()
            save_prediction_feedback(db, study_id=study.id, true_label=0)
            frame = read_confirmed_studies(
                db, date_from=date(2026, 9, 1), date_to=date(2026, 9, 1)
            )
            path = save_snapshot(frame, Path(folder))
            dataset = import_raw_dataset(db, path, name="audit_test")
            assert dataset.source_type == "feedback"
            save_prediction_feedback(db, study_id=study.id, true_label=1)
            history = db.scalars(
                select(FeedbackHistory)
                .where(FeedbackHistory.study_id == study.id)
                .order_by(FeedbackHistory.id)
            ).all()
            assert [(row.old_label, row.new_label) for row in history] == [
                (None, 0),
                (0, 1),
            ]
            assert (
                db.scalar(
                    text(
                        "SELECT outcome FROM dataset_rows WHERE dataset_id=:id AND source_study_id=:study_id"
                    ),
                    {"id": dataset.id, "study_id": study.id},
                )
                == 0
            )
            for statement, parameters in [
                ("UPDATE datasets SET row_count=1 WHERE id=:id", {"id": dataset.id}),
                ("UPDATE studies SET glucose=121 WHERE id=:id", {"id": study.id}),
                (
                    "UPDATE feedback_history SET new_label=1 WHERE study_id=:id",
                    {"id": study.id},
                ),
                (
                    "UPDATE feedback SET created_at=now() + interval '1 day' WHERE study_id=:id",
                    {"id": study.id},
                ),
                ("DELETE FROM feedback WHERE study_id=:id", {"id": study.id}),
            ]:
                try:
                    with db.begin_nested():
                        db.execute(text(statement), parameters)
                except DBAPIError:
                    pass
                else:
                    raise AssertionError("БД разрешила изменение неизменяемой записи")
            for column, invalid in [
                ("pregnancies", 21),
                ("glucose", 601),
                ("blood_pressure", 201),
                ("skin_thickness", 111),
                ("insulin", 1001),
                ("bmi", 101),
                ("diabetes_pedigree_function", 0),
                ("age", 0),
            ]:
                try:
                    with db.begin_nested():
                        db.add(
                            Study(
                                patient_code="AUD002",
                                study_date=date(2026, 9, 1),
                                features={**values, column: invalid},
                            )
                        )
                        db.flush()
                except DBAPIError:
                    pass
                else:
                    raise AssertionError(
                        f"БД пропустила недопустимое значение {column}"
                    )
            print(
                "Проверены границы, история обратной связи и ролей, неизменяемость снимка."
            )
        finally:
            db.rollback()


if __name__ == "__main__":
    main()
