from unittest.mock import MagicMock, Mock
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import SQLAlchemyError
from fastapi.testclient import TestClient
 
import src.app as app_module
from src.predict import DiabetesPredictor

TEST_PREDICTOR = DiabetesPredictor()
 
 
@pytest.fixture(autouse=True)
def mock_kafka_layer(monkeypatch):
    """
    Mocks the Kafka producer for API unit tests.
 
    Real Kafka publishing is verified separately through docker-compose and the
    CD functional tests. These unit tests validate API behavior only.
    """
    monkeypatch.setattr(
        app_module,
        "send_prediction_message",
        lambda *args, **kwargs: None,
    )
 
    yield
 
 
client = TestClient(app_module.app)


@pytest.fixture(autouse=True)
def mock_prediction_lookup(monkeypatch):
    """Изолирует проверку истории от настоящих PostgreSQL и Vault."""
    monkeypatch.setattr(app_module, "require_champion_model", lambda db: SimpleNamespace(model_version=TEST_PREDICTOR.model_version))
    monkeypatch.setattr(app_module.model_registry, "from_record", lambda record: TEST_PREDICTOR)
    db = MagicMock()
    db.__enter__.return_value = db
    db.execute.return_value.scalar_one_or_none.return_value = None
    db.execute.return_value.scalars.return_value.all.return_value = []
    monkeypatch.setattr(app_module, "get_session_factory", lambda: lambda: db)
    return db


VALID_INPUT = {
    "patient_code": "PAT001",
    "study_date": "2026-09-08",
    "pregnancies": 6,
    "glucose": 148,
    "blood_pressure": 72,
    "skin_thickness": 35,
    "insulin": 0,
    "bmi": 33.6,
    "diabetes_pedigree_function": 0.627,
    "age": 50,
}


def test_champion_switch_changes_predictor_and_message_version(monkeypatch):
    champions = iter([SimpleNamespace(model_version="v1"), SimpleNamespace(model_version="v2")])
    monkeypatch.setattr(app_module, "require_champion_model", lambda db: next(champions))
    results = {
        "v1": {"prediction": 0, "probability": 0.2, "label": "not_detected"},
        "v2": {"prediction": 1, "probability": 0.8, "label": "detected"},
    }
    monkeypatch.setattr(app_module.model_registry, "from_record",
                        lambda record: SimpleNamespace(predict=lambda data: results[record.model_version]))
    published = []
    monkeypatch.setattr(app_module, "send_prediction_message", lambda message, **kwargs: published.append(message))
    for version in ("v1", "v2"):
        response = client.post("/predict", json=VALID_INPUT)
        assert response.status_code == 200
        assert response.json() == results[version]
        assert published[-1]["model_version"] == version


def test_unloadable_champion_returns_503_without_publication(monkeypatch):
    def unavailable(record):
        raise ValueError("checksum mismatch")
    monkeypatch.setattr(app_module.model_registry, "from_record", unavailable)
    publish = Mock()
    monkeypatch.setattr(app_module, "send_prediction_message", publish)
    response = client.post("/predict", json=VALID_INPUT)
    assert response.status_code == 503
    assert response.json()["detail"] == "Основная модель временно недоступна. Повторите попытку позже."
    publish.assert_not_called()


def test_failed_kafka_publication_returns_503(monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError("broker unavailable")
    monkeypatch.setattr(app_module, "send_prediction_message", unavailable)
    response = client.post("/predict", json=VALID_INPUT)
    assert response.status_code == 503
    assert response.json()["detail"] == "Не удалось передать прогноз на сохранение. Повторите попытку позже."


def test_health_check_returns_ok_status():
    response = client.get("/health")

    assert response.status_code == 200

    data = response.json()

    assert data["status"] == "ok"
    assert data["service"] == "diabetes-prediction-api"


def test_predict_returns_valid_prediction():
    response = client.post("/predict", json=VALID_INPUT)

    assert response.status_code == 200

    data = response.json()

    assert "prediction" in data
    assert "probability" in data
    assert "label" in data

    assert data["prediction"] in [0, 1]
    assert data["label"] in ["detected", "not_detected"]

    if data["probability"] is not None:
        assert 0 <= data["probability"] <= 1


def test_predict_returns_422_for_missing_required_field():
    invalid_input = VALID_INPUT.copy()
    invalid_input.pop("age")

    response = client.post("/predict", json=invalid_input)

    assert response.status_code == 422


def test_predict_returns_422_for_negative_value():
    invalid_input = VALID_INPUT.copy()
    invalid_input["glucose"] = -1

    response = client.post("/predict", json=invalid_input)

    assert response.status_code == 422


def test_predict_returns_422_for_invalid_type():
    invalid_input = VALID_INPUT.copy()
    invalid_input["age"] = "fifty"

    response = client.post("/predict", json=invalid_input)

    assert response.status_code == 422


def test_unknown_endpoint_returns_404():
    response = client.get("/unknown")

    assert response.status_code == 404


def test_predict_rejects_zero_diabetes_pedigree_function():
    invalid_input = VALID_INPUT.copy()
    invalid_input["diabetes_pedigree_function"] = 0

    response = client.post("/predict", json=invalid_input)

    assert response.status_code == 422
    assert any(
        error["loc"] == ["body", "diabetes_pedigree_function"]
        and error["type"] == "greater_than"
        for error in response.json()["detail"]
    )


INPUT_RANGES = [
    ("pregnancies", 0, 20),
    ("glucose", 0, 600),
    ("blood_pressure", 0, 200),
    ("skin_thickness", 0, 110),
    ("insulin", 0, 1000),
    ("bmi", 0, 100),
    ("diabetes_pedigree_function", 0.001, 3),
    ("age", 1, 120),
]


@pytest.mark.parametrize("field,lower,upper", INPUT_RANGES)
@pytest.mark.parametrize("boundary", ["lower", "upper"])
def test_predict_accepts_range_boundaries(field, lower, upper, boundary):
    payload = VALID_INPUT.copy()
    payload[field] = lower if boundary == "lower" else upper

    response = client.post("/predict", json=payload)

    assert response.status_code == 200, response.text


@pytest.mark.parametrize("field,lower,upper", INPUT_RANGES)
@pytest.mark.parametrize("case", ["negative", "above_max", "null", "missing"])
def test_predict_rejects_invalid_feature_values(field, lower, upper, case):
    payload = VALID_INPUT.copy()
    if case == "missing":
        payload.pop(field)
    else:
        payload[field] = {
            "negative": -1,
            "above_max": upper + 1,
            "null": None,
        }[case]

    response = client.post("/predict", json=payload)

    assert response.status_code == 422
    assert any(
        error["loc"] == ["body", field]
        for error in response.json()["detail"]
    )


def test_predict_rejects_zero_age():
    payload = VALID_INPUT.copy()
    payload["age"] = 0

    response = client.post("/predict", json=payload)

    assert response.status_code == 422
    assert any(
        error["loc"] == ["body", "age"]
        and error["type"] == "greater_than"
        for error in response.json()["detail"]
    )


@pytest.mark.parametrize("code", [
    None, "", "   ", "\t\n", 123,
    "AB123", "ABCD123", "ABC12", "ABC1234",
    "123ABC", "AB1123", "ABCDEF", "123456",
    "ABC-123", "AB 123", "ПАТ001", "ABC１２３", "Aß001",
])
def test_predict_rejects_invalid_patient_code(code):
    payload = VALID_INPUT.copy()
    payload["patient_code"] = code

    response = client.post("/predict", json=payload)

    assert response.status_code == 422
    assert any(
        error["loc"] == ["body", "patient_code"]
        for error in response.json()["detail"]
    )


def test_predict_requires_patient_code():
    payload = VALID_INPUT.copy()
    payload.pop("patient_code")

    response = client.post("/predict", json=payload)

    assert response.status_code == 422
    assert any(
        error["loc"] == ["body", "patient_code"]
        and error["type"] == "missing"
        for error in response.json()["detail"]
    )


@pytest.mark.parametrize("code", ["PAT001", "ABC000", "XYZ999", "pat001", "PaT001", "  PAT001  "])
def test_predict_publishes_normalized_patient_code(monkeypatch, code):
    captured = {}

    def capture_message(message, key=None):
        captured["message"] = message
        captured["key"] = key

    monkeypatch.setattr(app_module, "send_prediction_message", capture_message)
    payload = VALID_INPUT.copy()
    payload["patient_code"] = code

    response = client.post("/predict", json=payload)

    assert response.status_code == 200, response.text
    assert captured["message"]["patient_code"] == code.strip().upper()
    assert captured["key"] == code.strip().upper()
    assert "patient_code" not in captured["message"]["features"]
    assert "study_date" not in captured["message"]["features"]
    assert captured["message"]["study_date"] == "2026-09-08"
    assert captured["message"]["model_version"] == TEST_PREDICTOR.model_version


@pytest.mark.parametrize("study_date", [
    None, "", "08.09.2026", "2026-02-30", "2026-09-08T00:00:00", 1788825600,
])
def test_predict_rejects_invalid_study_date(study_date):
    response = client.post("/predict", json={**VALID_INPUT, "study_date": study_date})

    assert response.status_code == 422
    assert any(
        error["loc"] == ["body", "study_date"]
        for error in response.json()["detail"]
    )


def test_predict_requires_study_date():
    payload = VALID_INPUT.copy()
    payload.pop("study_date")

    response = client.post("/predict", json=payload)

    assert response.status_code == 422
    assert any(
        error["loc"] == ["body", "study_date"] and error["type"] == "missing"
        for error in response.json()["detail"]
    )


def test_study_date_is_not_passed_to_prediction_model(monkeypatch):
    predict = Mock(return_value={"prediction": 1, "probability": 0.81, "label": "detected"})
    monkeypatch.setattr(TEST_PREDICTOR, "predict", predict)

    response = client.post("/predict", json=VALID_INPUT)

    assert response.status_code == 200
    predict.assert_called_once_with({
        key: value for key, value in VALID_INPUT.items()
        if key not in {"patient_code", "study_date"}
    })


def test_predict_returns_saved_result_without_prediction_or_publication(
    monkeypatch, mock_prediction_lookup,
):
    saved_result = {"prediction": 1, "probability": 0.8146575280180618, "label": "detected"}
    mock_prediction_lookup.execute.return_value.scalar_one_or_none.side_effect = [
        SimpleNamespace(id=19, features={k: v for k, v in VALID_INPUT.items() if k not in {"patient_code", "study_date"}}),
        SimpleNamespace(
            model_version_snapshot=TEST_PREDICTOR.model_version,
            **saved_result,
            inference_payload={
                "features": {k: v for k, v in VALID_INPUT.items() if k not in {"patient_code", "study_date"}},
                "result": saved_result,
            },
        )
    ]
    predict = Mock()
    publish = Mock()
    monkeypatch.setattr(TEST_PREDICTOR, "predict", predict)
    monkeypatch.setattr(app_module, "send_prediction_message", publish)

    response = client.post("/predict", json={**VALID_INPUT, "patient_code": " pat001 "})

    assert response.status_code == 200
    assert response.json() == saved_result
    predict.assert_not_called()
    publish.assert_not_called()


def test_predict_rejects_changed_study_even_for_another_model(
    monkeypatch, mock_prediction_lookup,
):
    features = {k: v for k, v in VALID_INPUT.items() if k not in {"patient_code", "study_date"}}
    features["glucose"] = 100
    mock_prediction_lookup.execute.return_value.scalar_one_or_none.return_value = SimpleNamespace(id=19, features=features)
    predict = Mock()
    publish = Mock()
    monkeypatch.setattr(TEST_PREDICTOR, "predict", predict)
    monkeypatch.setattr(app_module, "send_prediction_message", publish)

    response = client.post("/predict", json=VALID_INPUT)

    assert response.status_code == 409
    assert response.json()["detail"] == "На эту дату уже сохранено исследование с другими данными."
    predict.assert_not_called()
    publish.assert_not_called()


def test_predict_calculates_new_model_for_unchanged_study(monkeypatch, mock_prediction_lookup):
    features = {k: v for k, v in VALID_INPUT.items() if k not in {"patient_code", "study_date"}}
    mock_prediction_lookup.execute.return_value.scalar_one_or_none.side_effect = [
        SimpleNamespace(id=19, features=features), None,
    ]
    result = {"prediction": 0, "probability": 0.3, "label": "not_detected"}
    predict = Mock(return_value=result)
    publish = Mock()
    monkeypatch.setattr(TEST_PREDICTOR, "predict", predict)
    monkeypatch.setattr(app_module, "send_prediction_message", publish)

    response = client.post("/predict", json=VALID_INPUT)

    assert response.status_code == 200
    assert response.json() == result
    predict.assert_called_once_with(features)
    publish.assert_called_once()


@pytest.mark.parametrize("error", [RuntimeError("Vault unavailable"), SQLAlchemyError("DB unavailable")])
def test_predict_returns_503_when_duplicate_check_fails(monkeypatch, error):
    def unavailable():
        raise error

    predict = Mock()
    publish = Mock()
    monkeypatch.setattr(app_module, "get_session_factory", unavailable)
    monkeypatch.setattr(TEST_PREDICTOR, "predict", predict)
    monkeypatch.setattr(app_module, "send_prediction_message", publish)

    response = client.post("/predict", json=VALID_INPUT)

    assert response.status_code == 503
    assert response.json()["detail"] == "Не удалось подключиться к базе данных. Повторите попытку позже."
    predict.assert_not_called()
    publish.assert_not_called()
