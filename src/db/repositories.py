import re
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session
from src.features import FEATURE_COLUMNS
from src.schemas import DiabetesInput

from src.db.models import (
    ModelVersion,
    Patient,
    PredictionFeedback,
    PredictionHistory,
    Study,
)


class DuplicatePredictionError(RuntimeError):
    """Для этого кода пациента уже сохранён прогноз."""


class StudyConflictError(ValueError):
    """На дату исследования уже сохранены другие показатели пациента."""


class ChampionUnavailableError(RuntimeError):
    """В реестре отсутствует основная модель."""


def require_same_features(stored: dict, incoming: dict) -> None:
    if any(Decimal(str(stored[key])) != Decimal(str(incoming[key])) for key in FEATURE_COLUMNS):
        raise StudyConflictError("На эту дату уже сохранено исследование с другими данными.")


def get_study(db: Session, patient_code: str, study_date: date) -> Study | None:
    return db.execute(select(Study).where(
        Study.patient_code == patient_code.strip().upper(),
        Study.study_date == study_date,
    )).scalar_one_or_none()


def get_or_create_study(db: Session, patient_code: str, study_date: date, features: dict) -> Study:
    """Вызывать под транзакционной блокировкой пациента; commit делает вызывающий код."""
    validated = DiabetesInput(patient_code=patient_code, study_date=study_date, **features)
    patient_code = validated.patient_code
    features = validated.model_dump(exclude={"patient_code", "study_date"})
    study = get_study(db, patient_code, study_date)
    if study is not None:
        require_same_features(study.features, features)
        return study
    study = Study(patient_code=patient_code.strip().upper(), study_date=study_date,
                  features={key: features[key] for key in FEATURE_COLUMNS})
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
    record = db.execute(select(PredictionHistory).where(
        PredictionHistory.study_id == study.id,
        PredictionHistory.model_version_snapshot == model_version,
    )).scalar_one_or_none()
    if record is None:
        return None
    if record.inference_payload is not None:
        return dict(record.inference_payload["result"])
    return {
        "prediction": record.prediction,
        "probability": float(record.probability) if record.probability is not None else None,
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


def get_patient_by_code(db: Session, patient_code: str) -> Patient | None:
    """
    Ищет пациента по коду.
    """
    normalized_code = patient_code.strip()

    statement = select(Patient).where(Patient.patient_code == normalized_code)
    return db.execute(statement).scalar_one_or_none()


def get_or_create_patient(db: Session, patient_code: str | None) -> Patient | None:
    """
    Возвращает существующего пациента или создаёт нового.

    Если patient_code не передан, возвращает None.
    Это допустимо, потому что prediction_history.patient_id может быть NULL.
    """
    if patient_code is None:
        return None

    normalized_code = patient_code.strip()

    if not normalized_code:
        return None

    patient = get_patient_by_code(db, normalized_code)

    if patient is not None:
        return patient

    patient = Patient(patient_code=normalized_code)
    db.add(patient)
    db.flush()

    return patient


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
) -> PredictionHistory:
    """
    Сохраняет одно предсказание в prediction_history.

    Важно:
    - commit здесь не вызывается;
    - commit будет делать endpoint/service-слой;
    - это позволяет сохранить несколько связанных действий одной транзакцией.
    """
    if not isinstance(patient_code, str):
        raise ValueError("Patient code is required")
    patient_code = patient_code.strip()
    if not re.fullmatch(r"[A-Za-z]{3}[0-9]{3}", patient_code):
        raise ValueError("Patient code must contain three Latin letters and three digits")
    patient_code = patient_code.upper()

    if type(study_date) is not date:
        raise ValueError("Укажите дату исследования без времени")

    validated = DiabetesInput(patient_code=patient_code, study_date=study_date, **features)
    features = validated.model_dump(exclude={"patient_code", "study_date"})

    # Блокировка до конца транзакции: параллельные сообщения одного
    # пациента проверяются последовательно, включая создание patients.
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:patient_code, 0))"),
        {"patient_code": patient_code},
    )
    saved_result = get_prediction_for_study(
        db, patient_code=patient_code, study_date=study_date,
        model_version=model_version.model_version, features=features,
    )
    if saved_result is not None:
        raise DuplicatePredictionError("Это исследование уже сохранено для данной версии модели")

    patient = get_or_create_patient(db, patient_code)
    study = get_or_create_study(db, patient_code, study_date, features)

    label = "detected" if int(prediction) == 1 else "not_detected"

    prediction_history = PredictionHistory(
        study_id=study.id,
        user_id=user_id,
        patient_id=patient.id if patient is not None else None,
        model_version_id=model_version.id,
        patient_code_snapshot=patient_code,
        model_version_snapshot=model_version.model_version,
        study_date=study_date,
        inference_payload={
            "features": {key: features[key] for key in FEATURE_COLUMNS},
            "result": {"prediction": int(prediction), "probability": probability, "label": label},
        },
        pregnancies=features["pregnancies"],
        glucose=features["glucose"],
        blood_pressure=features["blood_pressure"],
        skin_thickness=features["skin_thickness"],
        insulin=features["insulin"],
        bmi=features["bmi"],
        diabetes_pedigree_function=features["diabetes_pedigree_function"],
        age=features["age"],
        prediction=int(prediction),
        probability=probability,
        label=label,
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
    Все версии моделей используют её через prediction_history.study_id.
    """
    if type(true_label) is not int or true_label not in (0, 1):
        raise ValueError("true_label must be 0 or 1")

    # Одна блокировка исследования сериализует параллельный ввод обратной связи.
    study = db.execute(select(Study).where(Study.id == study_id).with_for_update()).scalar_one_or_none()
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
        feedback.true_label = true_label
        feedback.created_by_user_id = created_by_user_id

    db.flush()

    return feedback
