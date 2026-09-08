import re
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from src.db.models import (
    ModelVersion,
    PredictionFeedback,
    PredictionHistory,
    Study,
)
from src.features import FEATURE_COLUMNS
from src.schemas import DiabetesInput, PredictionResponse


class DuplicatePredictionError(RuntimeError):
    """Для этого кода пациента уже сохранён прогноз."""


class StudyConflictError(ValueError):
    """На дату исследования уже сохранены другие показатели пациента."""


class ChampionUnavailableError(RuntimeError):
    """В реестре отсутствует основная модель."""


def require_same_features(stored: dict, incoming: dict) -> None:
    if any(
        Decimal(str(stored[key])) != Decimal(str(incoming[key]))
        for key in FEATURE_COLUMNS
    ):
        raise StudyConflictError(
            "На эту дату уже сохранено исследование с другими данными."
        )


def get_study(db: Session, patient_code: str, study_date: date) -> Study | None:
    return db.execute(
        select(Study).where(
            Study.patient_code == patient_code.strip().upper(),
            Study.study_date == study_date,
        )
    ).scalar_one_or_none()


def get_or_create_study(
    db: Session, patient_code: str, study_date: date, features: dict, created_by=None
) -> Study:
    """Фиксирует показатели под блокировкой пациента; commit делает вызывающий код."""
    validated = DiabetesInput(
        patient_code=patient_code, study_date=study_date, **features
    )
    patient_code = validated.patient_code
    features = validated.model_dump(exclude={"patient_code", "study_date"})
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:patient_code, 0))"),
        {"patient_code": patient_code},
    )
    study = get_study(db, patient_code, study_date)
    if study is not None:
        require_same_features(study.features, features)
        return study
    study = Study(
        patient_code=patient_code.strip().upper(),
        study_date=study_date,
        features={key: features[key] for key in FEATURE_COLUMNS},
        created_by=created_by,
    )
    db.add(study)
    db.flush()
    return study


def get_prediction_for_study(
    db: Session,
    *,
    patient_code: str,
    study_date: date,
    model_version: str,
    features: dict[str, Any],
) -> dict | None:
    """Проверяет показатели на дату и возвращает результат нужной версии модели."""
    study = get_study(db, patient_code, study_date)
    if study is None:
        return None
    require_same_features(study.features, features)
    record = db.execute(
        select(PredictionHistory)
        .join(ModelVersion)
        .where(
            PredictionHistory.study_id == study.id,
            ModelVersion.model_version == model_version,
        )
    ).scalar_one_or_none()
    if record is None:
        return None
    return {
        "prediction": record.prediction,
        "probability": float(record.probability)
        if record.probability is not None
        else None,
        "label": record.label,
    }


def get_champion_model(db: Session) -> ModelVersion | None:
    """
    Возвращает текущую champion-модель.

    В БД по ограничению uq_model_versions_single_champion
    может быть только одна модель с role = 'champion'.
    """
    statement = select(ModelVersion).where(ModelVersion.role == "champion")
    return db.execute(statement).scalar_one_or_none()


def require_champion_model(db: Session) -> ModelVersion:
    """
    Возвращает champion-модель или выбрасывает ошибку,
    если она не заведена в model_versions.
    """
    model_version = get_champion_model(db)

    if model_version is None:
        raise ChampionUnavailableError("Основная модель не назначена в реестре")

    return model_version


def require_model_version(db: Session, version: str) -> ModelVersion:
    if not isinstance(version, str) or not version.strip():
        raise ValueError("Не указана версия модели")
    model = db.execute(
        select(ModelVersion).where(ModelVersion.model_version == version)
    ).scalar_one_or_none()
    if model is None:
        raise ValueError("Версия модели не зарегистрирована в БД")
    return model


def save_prediction_history(
    db: Session,
    *,
    features: dict[str, Any],
    prediction: int,
    probability: float | None,
    model_version: ModelVersion,
    study_date: date,
    patient_code: str | None = None,
    user_id: int | None = None,
    request_source: str = "api",
    response_time_ms: int | None = None,
    role_at_prediction: str | None = None,
) -> PredictionHistory:
    """
    Сохраняет одно предсказание в prediction_history.

    Важно:
    - commit здесь не вызывается;
    - commit будет делать endpoint/service-слой;
    - это позволяет сохранить несколько связанных действий одной транзакцией.
    """
    if not isinstance(patient_code, str):
        raise ValueError("Укажите код пациента")
    patient_code = patient_code.strip()
    if not re.fullmatch(r"[A-Za-z]{3}[0-9]{3}", patient_code):
        raise ValueError(
            "Код пациента должен содержать три латинские буквы и три цифры"
        )
    patient_code = patient_code.upper()

    if type(study_date) is not date:
        raise ValueError("Укажите дату исследования без времени")

    validated = DiabetesInput(
        patient_code=patient_code, study_date=study_date, **features
    )
    features = validated.model_dump(exclude={"patient_code", "study_date"})

    # Блокировка до конца транзакции: параллельные сообщения одного
    # пациента проверяются последовательно, включая создание исследования.
    PredictionResponse(
        prediction=prediction,
        probability=probability,
        label="detected" if prediction == 1 else "not_detected",
    )
    if (role_at_prediction or model_version.role) not in {"champion", "challenger"}:
        raise ValueError("Не указана роль модели на момент прогноза")
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:patient_code, 0))"),
        {"patient_code": patient_code},
    )
    saved_result = get_prediction_for_study(
        db,
        patient_code=patient_code,
        study_date=study_date,
        model_version=model_version.model_version,
        features=features,
    )
    if saved_result is not None:
        raise DuplicatePredictionError(
            "Это исследование уже сохранено для данной версии модели"
        )

    study = get_or_create_study(
        db, patient_code, study_date, features, created_by=user_id
    )

    prediction_history = PredictionHistory(
        study_id=study.id,
        model_version_id=model_version.id,
        prediction=int(prediction),
        probability=probability,
        role_at_prediction=role_at_prediction or model_version.role,
        request_source=request_source,
        response_time_ms=response_time_ms,
    )

    db.add(prediction_history)
    db.flush()

    return prediction_history


def get_prediction_history_by_id(
    db: Session,
    prediction_history_id: int,
) -> PredictionHistory | None:
    """
    Возвращает запись истории предсказания по id.
    """
    statement = select(PredictionHistory).where(
        PredictionHistory.id == prediction_history_id
    )
    return db.execute(statement).scalar_one_or_none()


def save_prediction_feedback(
    db: Session,
    *,
    study_id: int,
    true_label: int,
    created_by_user_id: int | None = None,
) -> PredictionFeedback:
    """
    Создаёт или обновляет общую фактическую метку исследования.
    Все версии моделей используют её через predictions.study_id.
    """
    if type(true_label) is not int or true_label not in (0, 1):
        raise ValueError("Фактическая метка должна быть 0 или 1")

    # Одна блокировка исследования сериализует параллельный ввод обратной связи.
    study = db.execute(
        select(Study).where(Study.id == study_id).with_for_update()
    ).scalar_one_or_none()
    if study is None:
        raise ValueError("Исследование не найдено")

    statement = select(PredictionFeedback).where(
        PredictionFeedback.study_id == study_id
    )
    feedback = db.execute(statement).scalar_one_or_none()

    if feedback is None:
        feedback = PredictionFeedback(
            study_id=study_id,
            true_label=true_label,
            created_by_user_id=created_by_user_id,
        )
        db.add(feedback)
    else:
        if feedback.true_label == true_label:
            return feedback
        feedback.true_label = true_label
        feedback.created_by_user_id = created_by_user_id

    db.flush()

    return feedback
