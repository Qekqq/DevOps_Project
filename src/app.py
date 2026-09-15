import time
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from src.auth import current_user
from src.auth import router as auth_router
from src.config import load_config
from src.db.database import get_session_factory
from src.db.models import User
from src.db.repositories import (
    ChampionUnavailableError,
    StudyConflictError,
    get_or_create_study,
    get_prediction_for_study,
    require_champion_model,
)
from src.kafka.producer import send_prediction_message
from src.logger import get_logger
from src.model_registry import ModelRegistry
from src.monitoring import router as monitoring_router
from src.schemas import DiabetesInput, HealthResponse, PredictionResponse
from src.studies import router as studies_router
from src.telemetry import (
    PREDICTIONS,
    event,
    lifespan,
    measure_inference,
    observe_request,
    request_id,
)

logger = get_logger(__name__)

app = FastAPI(
    root_path="/api",
    title="API прогнозирования диабета",
    description="API для предсказания наличия диабета на основе медицинских признаков.",
    version="1.0.0",
    lifespan=lifespan,
)
app.middleware("http")(observe_request)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in load_config().get("api", "cors_origins", fallback="").split(",")
        if origin.strip()
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH"],
    allow_headers=["Content-Type", "Authorization"],
)

model_registry = ModelRegistry()
app.include_router(auth_router)
app.include_router(studies_router)
app.include_router(monitoring_router)


@app.exception_handler(RequestValidationError)
async def input_validation_error(request, error):
    """Сохраняет машинные type/loc, переводит сообщения и не возвращает входные данные."""
    messages = {
        "missing": "Обязательное поле не заполнено",
        "extra_forbidden": "Неизвестное поле",
        "string_type": "Ожидается строка",
        "string_too_short": "Недостаточная длина значения",
        "string_too_long": "Превышена допустимая длина значения",
        "string_pattern_mismatch": "Код пациента должен содержать три латинские буквы и три цифры",
        "int_parsing": "Ожидается целое число",
        "int_from_float": "Ожидается целое число",
        "int_type": "Ожидается целое число",
        "float_type": "Ожидается число",
        "float_parsing": "Ожидается число",
        "date_from_datetime_parsing": "Укажите существующую дату в формате ГГГГ-ММ-ДД",
        "json_invalid": "Некорректный JSON",
    }
    details = []
    for item in error.errors():
        kind, context = item["type"], item.get("ctx", {})
        message = messages.get(kind, "Некорректное значение поля")
        for code, operator in (
            ("greater_than", ">"),
            ("greater_than_equal", ">="),
            ("less_than", "<"),
            ("less_than_equal", "<="),
        ):
            if kind == code:
                bound = context.get(
                    {">": "gt", ">=": "ge", "<": "lt", "<=": "le"}[operator]
                )
                message = f"Значение должно быть {operator} {bound}"
        if kind == "value_error":
            message = str(context.get("error", message))
        details.append({"loc": item["loc"], "type": kind, "msg": message})
    return JSONResponse(status_code=422, content={"detail": details})


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
    model_version: str,
    predicted_at: datetime | None = None,
) -> dict:
    """
    Формирует сообщение с результатом работы модели для отправки в Kafka.
    """
    return {
        "patient_code": input_data.patient_code,
        "study_date": input_data.study_date.isoformat(),
        "model_version": model_version,
        "role_at_prediction": "champion",
        "features": build_features(input_data),
        "prediction": result["prediction"],
        "probability": result.get("probability"),
        "label": result["label"],
        "response_time_ms": response_time_ms,
        "created_at": (predicted_at or datetime.now(timezone.utc)).isoformat(),
        "request_id": request_id.get(),
    }


def publish_prediction(
    input_data: DiabetesInput,
    result: dict,
    response_time_ms: int,
    model_version: str,
    predicted_at: datetime | None = None,
) -> None:
    """
    Публикует результат прогноза в Kafka (роль Producer).

    При недоступной Kafka запрос не считается принятым на сохранение.
    """
    try:
        message = build_prediction_message(
            input_data, result, response_time_ms, model_version, predicted_at
        )
        send_prediction_message(message, key=input_data.patient_code)
    except Exception as error:  # noqa: BLE001
        logger.error(
            "Не удалось опубликовать прогноз в Kafka: %s", type(error).__name__
        )
        raise HTTPException(
            status_code=503,
            detail="Не удалось передать прогноз на сохранение. Повторите попытку позже.",
        ) from error


@app.get("/health", response_model=HealthResponse, summary="Состояние API")
def health_check() -> HealthResponse:
    """
    Проверяет состояние API.
    """
    return HealthResponse(status="ok", service="diabetes-prediction-api")


@app.get("/db/health", summary="Подключение к базе данных")
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
        logger.error(
            "Проверка подключения к БД завершилась ошибкой: %s", type(error).__name__
        )
        raise HTTPException(
            status_code=503,
            detail="Не удалось подключиться к базе данных. Повторите попытку позже.",
        )


@app.post("/predict", response_model=PredictionResponse, summary="Получить прогноз")
def predict_diabetes(
    input_data: DiabetesInput, user: User = Depends(current_user)
) -> PredictionResponse:
    """
    Выполняет прогноз риска диабета и публикует результат в Kafka.
    """
    start_time = time.perf_counter()
    predicted_at = datetime.now(timezone.utc)
    model_input = build_features(input_data)

    try:
        session_factory = get_session_factory()
        with session_factory() as db:
            champion = require_champion_model(db)
            selected_version = champion.model_version
            saved_result = get_prediction_for_study(
                db,
                patient_code=input_data.patient_code,
                study_date=input_data.study_date,
                model_version=selected_version,
                features=model_input,
            )
    except StudyConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ChampionUnavailableError as error:
        raise HTTPException(
            status_code=503,
            detail="Основная модель не назначена. Обратитесь к администратору.",
        ) from error
    except (RuntimeError, SQLAlchemyError) as error:
        logger.error("Не удалось проверить наличие прогноза: %s", type(error).__name__)
        raise HTTPException(
            status_code=503,
            detail="Не удалось подключиться к базе данных. Повторите попытку позже.",
        ) from error

    if saved_result is not None:
        PREDICTIONS.labels(cached="true").inc()
        event("prediction_returned", cached=True, actor_id=user.id)
        return PredictionResponse(**saved_result, cached=True)

    try:
        predictor = model_registry.from_record(champion)
    except Exception as error:
        logger.error("Не удалось загрузить champion: %s", type(error).__name__)
        raise HTTPException(
            status_code=503,
            detail="Основная модель временно недоступна. Повторите попытку позже.",
        ) from error

    try:
        with measure_inference("champion", "predict"):
            result = predictor.predict(model_input)

        # Фиксируем исследование до публикации: второй запрос с другими
        # показателями получает 409 даже пока consumer ещё не записал прогноз.
        try:
            with session_factory() as db:
                get_or_create_study(
                    db,
                    input_data.patient_code,
                    input_data.study_date,
                    model_input,
                    created_by=user.id,
                )
                db.commit()
        except StudyConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (RuntimeError, SQLAlchemyError) as error:
            raise HTTPException(
                status_code=503,
                detail="Не удалось сохранить исследование. Повторите попытку позже.",
            ) from error

        response_time_ms = int((time.perf_counter() - start_time) * 1000)

        publish_prediction(
            input_data=input_data,
            result=result,
            response_time_ms=response_time_ms,
            model_version=selected_version,
            predicted_at=predicted_at,
        )

        PREDICTIONS.labels(cached="false").inc()
        event("prediction_published", cached=False, actor_id=user.id)
        return PredictionResponse(**result)

    except HTTPException:
        raise
    except ValueError as error:
        logger.error(
            "Ошибка валидации при выполнении прогноза: %s", type(error).__name__
        )
        raise HTTPException(
            status_code=400,
            detail="Не удалось обработать данные для прогноза. Проверьте введённые значения.",
        )

    except Exception as error:
        logger.error(
            "Непредвиденная ошибка при выполнении прогноза: %s", type(error).__name__
        )
        raise HTTPException(
            status_code=500,
            detail="Не удалось выполнить прогноз из-за внутренней ошибки сервиса.",
        )
