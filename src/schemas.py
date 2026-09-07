from pydantic import BaseModel, Field, field_validator


class DiabetesInput(BaseModel):
    """
    Схема входных данных для предсказания диабета.
    """

    patient_code: str | None = Field(
        default=None,
        max_length=100,
        description="Необязательный код пациента.",
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

    @field_validator("patient_code")
    @classmethod
    def validate_patient_code(cls, value: str | None) -> str | None:
        """
        Убирает лишние пробелы из кода пациента.

        Если код пациента не передан, возвращает None.
        """
        if value is None:
            return None

        value = value.strip()

        if not value:
            return None

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
