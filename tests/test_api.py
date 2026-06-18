import pytest
from fastapi.testclient import TestClient
 
import src.app as app_module
 
 
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


VALID_INPUT = {
    "pregnancies": 6,
    "glucose": 148,
    "blood_pressure": 72,
    "skin_thickness": 35,
    "insulin": 0,
    "bmi": 33.6,
    "diabetes_pedigree_function": 0.627,
    "age": 50,
}


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