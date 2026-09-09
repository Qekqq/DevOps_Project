"""Карточка исследования и административная обратная связь."""

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select, true
from sqlalchemy.orm import Session, joinedload

from src.auth import current_user, require_admin
from src.config import get_project_root
from src.db.database import get_db
from src.db.load_raw_dataset import import_raw_dataset
from src.db.models import (
    ModelVersion,
    PredictionFeedback,
    PredictionHistory,
    Study,
    User,
)
from src.db.repositories import get_study, save_prediction_feedback
from src.feedback_dataset import (
    read_confirmed_studies,
    save_snapshot,
    validate_snapshot_name,
)
from src.study_editing import StudyUpdate, update_study

router = APIRouter(prefix="/studies", tags=["Прогнозы"])


@router.put(
    "/{patient_code}/{study_date}", summary="Изменить показатели и пересчитать прогнозы"
)
def edit_study(
    patient_code: str,
    study_date: date,
    data: StudyUpdate,
    response: Response,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    return update_study(db, user, patient_code, study_date, data)


class FeedbackInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    true_label: int = Field(strict=True, ge=0, le=1)


def period_conditions(date_from, date_to):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "Начало периода не может быть позже окончания")
    conditions = []
    if date_from:
        conditions.append(Study.study_date >= date_from)
    if date_to:
        conditions.append(Study.study_date <= date_to)
    return conditions


def prediction_data(item):
    model = item.model_version
    names = {
        "logistic_regression": "Logistic Regression",
        "decision_tree": "Decision Tree",
    }
    return {
        "model_name": names.get(getattr(model, "family", None), model.model_name),
        "model_version": model.model_version,
        "display_version": model_display_version(model),
        "role": item.role_at_prediction,
        "prediction": item.prediction,
        "probability": item.probability,
        "status": "ready",
    }


def model_display_version(model):
    prefix = {"logistic_regression": "LR", "decision_tree": "DT"}.get(
        getattr(model, "family", None), "M"
    )
    return f"{prefix}-{model.id:02d}"


@router.get("", summary="История прогнозов")
def study_history(
    response: Response,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
    patient: str = Query(default="", max_length=6, pattern="^[a-zA-Z0-9]*$"),
    date_from: date | None = None,
    date_to: date | None = None,
    feedback: Literal["all", "missing", "filled"] = "all",
    model: list[str] = Query(default=["champion"]),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
):
    conditions = period_conditions(date_from, date_to)
    if patient:
        conditions.append(Study.patient_code.contains(patient.upper()))
    has_feedback = (
        select(PredictionFeedback.id)
        .where(PredictionFeedback.study_id == Study.id)
        .exists()
    )
    if feedback != "all":
        conditions.append(has_feedback if feedback == "filled" else ~has_feedback)
    active = db.scalars(
        select(ModelVersion)
        .where(ModelVersion.role.in_(["champion", "challenger"]))
        .order_by(ModelVersion.id)
    ).all()
    allowed = {str(item.id) for item in active} | {"champion", "all"}
    if not model or any(value not in allowed for value in model):
        raise HTTPException(422, "Выберите доступную активную модель")
    selected = [
        item.id
        for item in active
        if "all" in model
        or str(item.id) in model
        or ("champion" in model and item.role == "champion")
    ]
    study_total = db.scalar(select(func.count()).select_from(Study).where(*conditions))
    filled = db.scalar(
        select(func.count()).select_from(Study).where(*conditions, has_feedback)
    )
    total = study_total * len(selected)
    page = min(page, max(1, (total + page_size - 1) // page_size))
    rows = db.execute(
        select(Study, ModelVersion, PredictionHistory, PredictionFeedback.true_label)
        .select_from(Study)
        .join(ModelVersion, true())
        .outerjoin(
            PredictionHistory,
            (PredictionHistory.study_id == Study.id)
            & (PredictionHistory.model_version_id == ModelVersion.id),
        )
        .outerjoin(PredictionFeedback, PredictionFeedback.study_id == Study.id)
        .where(*conditions, ModelVersion.id.in_(selected))
        .order_by(Study.study_date.desc(), Study.id.desc(), ModelVersion.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    response.headers["Cache-Control"] = "no-store"
    return {
        "items": [
            {
                "id": study.id,
                "patient": study.patient_code,
                "date": study.study_date,
                "model": {
                    **model_data(version),
                    "prediction": prediction.prediction if prediction else None,
                    "probability": prediction.probability if prediction else None,
                },
                "feedback": label,
            }
            for study, version, prediction, label in rows
        ],
        "models": [model_data(item) for item in active],
        "total": total,
        "study_total": study_total if selected else 0,
        "filled": filled if selected else 0,
        "page": page,
        "page_size": page_size,
    }


def model_data(model):
    names = {
        "logistic_regression": "Logistic Regression",
        "decision_tree": "Decision Tree",
    }
    return {
        "id": model.id,
        "model_name": names.get(getattr(model, "family", None), model.model_name),
        "model_version": model.model_version,
        "display_version": model_display_version(model),
        "role": model.role,
    }


class SnapshotInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, value):
        return validate_snapshot_name(value)


@router.post("/snapshot", summary="Создать снимок исследований с обратной связью")
def create_snapshot(
    data: SnapshotInput,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
    date_from: date | None = None,
    date_to: date | None = None,
):
    period_conditions(date_from, date_to)
    frame = read_confirmed_studies(db, date_from=date_from, date_to=date_to)
    if frame.empty:
        raise HTTPException(
            404, "Нет исследований с обратной связью за выбранный период"
        )
    path = save_snapshot(
        frame,
        get_project_root() / "data" / "feedback",
        date_from=date_from,
        date_to=date_to,
        name=data.name,
    )
    import_raw_dataset(db, path, name=data.name)
    db.commit()
    return FileResponse(
        path,
        media_type="text/csv",
        filename=f"snapshot-{data.name}.csv",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/{patient_code}/{study_date}", summary="Карточка исследования")
def study_detail(
    patient_code: str,
    study_date: date,
    response: Response,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    study = get_study(db, patient_code, study_date)
    if study is None:
        raise HTTPException(404, "Исследование не найдено")
    saved = db.scalars(
        select(PredictionHistory)
        .options(joinedload(PredictionHistory.model_version))
        .where(PredictionHistory.study_id == study.id)
    ).all()
    predictions = [prediction_data(item) for item in saved]
    # Consumer записывает результаты асинхронно. Не подменяем их примерами.
    saved_ids = {item.model_version_id for item in saved}
    active = db.scalars(
        select(ModelVersion).where(ModelVersion.role.in_(["champion", "challenger"]))
    ).all()
    predictions.extend(
        {
            "model_name": model.model_name,
            "model_version": model.model_version,
            "display_version": model_display_version(model),
            "role": model.role,
            "prediction": None,
            "probability": None,
            "status": "pending",
        }
        for model in active
        if model.id not in saved_ids
    )
    feedback = None
    if user.role == "admin":
        feedback = db.scalar(
            select(PredictionFeedback).where(PredictionFeedback.study_id == study.id)
        )
    response.headers["Cache-Control"] = "no-store"
    return {
        "id": study.id,
        "patient": study.patient_code,
        "date": study.study_date,
        "features": study.features,
        "predictions": sorted(predictions, key=lambda p: p["role"] != "champion"),
        "feedback": feedback.true_label if feedback is not None else None,
        "can_feedback": user.role == "admin",
    }


@router.put(
    "/{patient_code}/{study_date}/feedback", summary="Сохранить фактический исход"
)
def study_feedback(
    patient_code: str,
    study_date: date,
    data: FeedbackInput,
    response: Response,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    study = get_study(db, patient_code, study_date)
    if study is None:
        raise HTTPException(404, "Исследование не найдено")
    feedback = save_prediction_feedback(
        db, study_id=study.id, true_label=data.true_label, created_by_user_id=user.id
    )
    label = feedback.true_label
    db.commit()
    from src.telemetry import event

    event("feedback_saved", actor_id=user.id, feedback_changed=True)
    response.headers["Cache-Control"] = "no-store"
    return {"feedback": label}
