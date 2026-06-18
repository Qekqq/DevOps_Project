import time
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from src.db.database import get_session_factory
from src.kafka.producer import send_prediction_message
from src.logger import get_logger
from src.predict import DiabetesPredictor
from src.schemas import DiabetesInput, HealthResponse, PredictionResponse


logger = get_logger(__name__)

app = FastAPI(
    title="Diabetes Prediction API",
    description="API для предсказания наличия диабета на основе медицинских признаков.",
    version="1.0.0",
)

predictor = DiabetesPredictor()


def build_features(input_data: DiabetesInput) -> dict[str, float | int]:
    """
    Преобразует входные данные API в словарь признаков.
    """
    return {
        "pregnancies": input_data.pregnancies,
        "glucose": input_data.glucose,
        "blood_pressure": input_data.blood_pressure,
        "skin_thickness": input_data.skin_thickness,
        "insulin": input_data.insulin,
        "bmi": input_data.bmi,
        "diabetes_pedigree_function": input_data.diabetes_pedigree_function,
        "age": input_data.age,
    }


def build_prediction_message(
    input_data: DiabetesInput,
    result: dict,
    response_time_ms: int,
) -> dict:
    """
    Формирует сообщение с результатом работы модели для отправки в Kafka.
    """
    return {
        "patient_code": input_data.patient_code,
        "features": build_features(input_data),
        "prediction": result["prediction"],
        "probability": result.get("probability"),
        "label": result["label"],
        "request_source": "api",
        "response_time_ms": response_time_ms,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def publish_prediction(
    input_data: DiabetesInput,
    result: dict,
    response_time_ms: int,
) -> None:
    """
    Публикует результат прогноза в Kafka (роль Producer).

    Если Kafka недоступна, API всё равно возвращает результат прогноза.
    """
    try:
        message = build_prediction_message(input_data, result, response_time_ms)
        send_prediction_message(message, key=input_data.patient_code)
    except Exception as error:  # noqa: BLE001
        logger.error("Не удалось опубликовать прогноз в Kafka: %s", error)


@app.get("/health", response_model=HealthResponse)
def health_check() -> HealthResponse:
    """
    Проверяет состояние API.
    """
    return HealthResponse(status="ok", service="diabetes-prediction-api")


@app.get("/db/health")
def database_health_check() -> dict[str, str]:
    """
    Проверяет подключение API к PostgreSQL.
    """
    try:
        session_factory = get_session_factory()

        with session_factory() as db:
            db.execute(text("SELECT 1"))

        return {"status": "ok", "database": "connected"}

    except (RuntimeError, SQLAlchemyError) as error:
        logger.error("Проверка подключения к БД завершилась ошибкой: %s", error)
        raise HTTPException(status_code=503, detail="Database connection failed")


@app.post("/predict", response_model=PredictionResponse)
def predict_diabetes(input_data: DiabetesInput) -> PredictionResponse:
    """
    Выполняет прогноз риска диабета и публикует результат в Kafka.
    """
    start_time = time.perf_counter()

    try:
        model_input = input_data.model_dump(exclude={"patient_code"})
        result = predictor.predict(model_input)

        response_time_ms = int((time.perf_counter() - start_time) * 1000)

        publish_prediction(
            input_data=input_data,
            result=result,
            response_time_ms=response_time_ms,
        )

        return PredictionResponse(**result)

    except ValueError as error:
        logger.error("Ошибка валидации при выполнении прогноза: %s", error)
        raise HTTPException(status_code=400, detail=str(error))

    except Exception as error:
        logger.error("Непредвиденная ошибка при выполнении прогноза: %s", error)
        raise HTTPException(status_code=500, detail="Internal server error")