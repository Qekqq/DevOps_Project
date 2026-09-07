from datetime import date
import re

from pydantic import BaseModel, Field, field_validator


class DiabetesInput(BaseModel):
    """
    Схема входных данных для предсказания диабета.
    """

    patient_code: str = Field(
        ...,
        min_length=6,
        max_length=6,
        pattern=r"^[A-Z]{3}[0-9]{3}$",
        description="Код пациента: три латинские буквы и три цифры, например PAT001.",
        examples=["PAT001"],
    )
    study_date: date = Field(
        ...,
        description="Дата исследования без времени в формате ГГГГ-ММ-ДД.",
        examples=["2026-09-08"],
    )
    pregnancies: int = Field(..., ge=0, le=20, description="Количество беременностей.")
    glucose: float = Field(..., ge=0, le=600, description="Уровень глюкозы.")
    blood_pressure: float = Field(..., ge=0, le=200, description="Артериальное давление.")
    skin_thickness: float = Field(..., ge=0, le=110, description="Толщина кожной складки.")
    insulin: float = Field(..., ge=0, le=1000, description="Уровень инсулина.")
    bmi: float = Field(..., ge=0, le=100, description="Индекс массы тела.")
    diabetes_pedigree_function: float = Field(
        ...,
        gt=0,
        le=3,
        description="Наследственный фактор диабета.",
    )
    age: int = Field(..., gt=0, le=120, description="Возраст.")

    @field_validator("study_date", mode="before")
    @classmethod
    def validate_study_date(cls, value: object) -> object:
        """Принимает календарную дату; время и числовые timestamps запрещены."""
        if type(value) is date:
            return value
        if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            return value
        raise ValueError("Укажите дату исследования без времени в формате ГГГГ-ММ-ДД")

    @field_validator("patient_code", mode="before")
    @classmethod
    def validate_patient_code(cls, value: object) -> object:
        """
        Убирает пробелы по краям и приводит латинские буквы к верхнему регистру.
        Тип, длина и формат затем проверяются ограничениями поля.
        """
        if isinstance(value, str):
            value = value.strip()
            if value.isascii():
                value = value.upper()

        return value


class PredictionResponse(BaseModel):
    """
    Схема ответа API с результатом предсказания.
    """

    prediction: int = Field(..., description="Класс предсказания: 0 или 1.")
    probability: float | None = Field(
        default=None,
        description="Вероятность положительного класса.",
    )
    label: str = Field(..., description="Текстовая интерпретация результата.")


class HealthResponse(BaseModel):
    """
    Схема ответа для проверки состояния API.
    """

    status: str
    service: str
