"""Атомарное исправление показателей и пересчёт результатов моделей."""

import time

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text

from src.db.models import ModelVersion, PredictionFeedback, PredictionHistory, StudyEdit
from src.db.repositories import (
    StudyConflictError,
    get_study,
    require_same_features,
    save_prediction_feedback,
)
from src.model_registry import ModelRegistry
from src.schemas import DiabetesInput, PredictionResponse
from src.telemetry import event, measure_inference

registry = ModelRegistry()


class StudyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: DiabetesInput
    original: DiabetesInput
    true_label: int | None = Field(default=None, strict=True, ge=0, le=1)
    original_feedback: int | None = Field(default=None, strict=True, ge=0, le=1)


def update_study(db, user, patient_code, study_date, data):
    if any(
        value.patient_code != patient_code or value.study_date != study_date
        for value in (data.input, data.original)
    ):
        raise HTTPException(422, "Код пациента и дату исследования менять нельзя")
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:patient_code, 0))"),
        {"patient_code": patient_code},
    )
    study = get_study(db, patient_code, study_date)
    if study is None:
        raise HTTPException(404, "Исследование не найдено")
    # Такой же порядок блокировок используется consumer и записью обратной связи.
    db.refresh(study, with_for_update=True)
    exclude = {"patient_code", "study_date"}
    before = study.features
    features = data.input.model_dump(exclude=exclude)
    try:
        require_same_features(before, data.original.model_dump(exclude=exclude))
    except StudyConflictError as error:
        raise HTTPException(
            409, "Показатели уже изменены. Откройте карточку заново."
        ) from error
    feedback = db.scalar(
        select(PredictionFeedback).where(PredictionFeedback.study_id == study.id)
    )
    if (feedback.true_label if feedback else None) != data.original_feedback:
        raise HTTPException(
            409, "Обратная связь уже изменена. Откройте карточку заново."
        )
    try:
        require_same_features(before, features)
        changed = False
    except StudyConflictError:
        changed = True
    feedback_changed = (
        data.true_label is not None and data.true_label != data.original_feedback
    )
    champion_result = None
    if changed:
        saved = db.scalars(
            select(PredictionHistory).where(PredictionHistory.study_id == study.id)
        ).all()
        models = db.scalars(
            select(ModelVersion).where(
                (ModelVersion.role.in_(["champion", "challenger"]))
                | (ModelVersion.id.in_([item.model_version_id for item in saved]))
            )
        ).all()
        if not any(model.role == "champion" for model in models):
            raise HTTPException(503, "Основная модель не назначена")
        calculated = []
        # Сначала рассчитываем всё: ошибка любой модели откатывает всю правку.
        try:
            for model in models:
                start = time.perf_counter()
                predictor = registry.from_record(model)
                with measure_inference(model.role, "recalculate"):
                    result = PredictionResponse(**predictor.predict(features))
                calculated.append(
                    (model, result, int((time.perf_counter() - start) * 1000))
                )
        except Exception as error:
            raise HTTPException(
                503, "Пересчёт моделей не выполнен. Изменения не сохранены."
            ) from error
        champion_result = next(
            result.model_dump()
            for model, result, _ in calculated
            if model.role == "champion"
        )
        db.add(
            StudyEdit(
                study_id=study.id,
                changed_by=user.id,
                features_before=before,
                features_after=features,
                predictions_before=[
                    {
                        "model_version_id": item.model_version_id,
                        "prediction": item.prediction,
                        "probability": item.probability,
                        "role": item.role_at_prediction,
                    }
                    for item in saved
                ],
            )
        )
        db.execute(text("SET LOCAL app.recalculate_study = 'on'"))
        study.features = features
        by_model = {item.model_version_id: item for item in saved}
        for model, result, elapsed in calculated:
            record = by_model.get(model.id)
            if record is None:
                record = PredictionHistory(
                    study_id=study.id,
                    model_version_id=model.id,
                    role_at_prediction=model.role,
                )
                db.add(record)
            record.prediction = result.prediction
            record.probability = result.probability
            record.response_time_ms = elapsed
        db.flush()
    if data.true_label is not None:
        save_prediction_feedback(
            db,
            study_id=study.id,
            true_label=data.true_label,
            created_by_user_id=user.id,
        )
    db.commit()
    event(
        "study_saved",
        actor_id=user.id,
        recalculated=changed,
        feedback_changed=feedback_changed,
    )
    return {
        "recalculated": changed,
        "feedback_changed": feedback_changed,
        "champion": champion_result,
    }
