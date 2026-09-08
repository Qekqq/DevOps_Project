from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from src.kafka import shadow


@pytest.mark.parametrize("already_saved", [False, True])
def test_shadow_preserves_study_identity_and_skips_saved_prediction(
    monkeypatch, already_saved
):
    db = MagicMock()
    db.__enter__.return_value = db
    db.execute.return_value.scalars.return_value.all.return_value = ["background-v1"]
    model = SimpleNamespace(model_version="background-v1")
    db.execute.return_value.scalar_one_or_none.return_value = model
    monkeypatch.setattr(shadow, "get_session_factory", lambda: lambda: db)
    monkeypatch.setattr(
        shadow,
        "get_prediction_for_study",
        lambda *args, **kwargs: {} if already_saved else None,
    )
    predictor = Mock()
    predictor.predict.return_value = {"prediction": 1, "probability": 0.8}
    monkeypatch.setattr(shadow.registry, "from_record", lambda record: predictor)
    save = Mock()
    monkeypatch.setattr(shadow, "save_prediction_history", save)
    message = {
        "patient_code": "PAT001",
        "study_date": "2026-09-08",
        "model_version": "champion-v1",
        "features": {"glucose": 120},
    }
    shadow.predict_challengers(message)
    if already_saved:
        predictor.predict.assert_not_called()
        save.assert_not_called()
    else:
        assert save.call_args.kwargs["patient_code"] == "PAT001"
        assert save.call_args.kwargs["study_date"].isoformat() == message["study_date"]
        assert save.call_args.kwargs["model_version"] is model
        db.commit.assert_called_once()


def test_failed_model_does_not_prevent_other_models_from_being_saved(monkeypatch):
    db = MagicMock()
    db.__enter__.return_value = db
    db.execute.return_value.scalars.return_value.all.return_value = [
        "broken",
        "working",
    ]
    db.execute.return_value.scalar_one_or_none.side_effect = [
        SimpleNamespace(model_version="broken"),
        SimpleNamespace(model_version="working"),
    ]
    monkeypatch.setattr(shadow, "get_session_factory", lambda: lambda: db)
    monkeypatch.setattr(
        shadow, "get_prediction_for_study", lambda *args, **kwargs: None
    )

    def load(record):
        if record.model_version == "broken":
            raise ValueError("invalid artifact")
        return SimpleNamespace(
            predict=lambda features: {"prediction": 0, "probability": 0.2}
        )

    monkeypatch.setattr(shadow.registry, "from_record", load)
    save = Mock()
    monkeypatch.setattr(shadow, "save_prediction_history", save)
    with pytest.raises(RuntimeError, match="broken"):
        shadow.predict_challengers(
            {
                "patient_code": "PAT001",
                "study_date": "2026-09-08",
                "model_version": "champion",
                "features": {},
            }
        )
    assert save.call_args.kwargs["model_version"].model_version == "working"
    db.commit.assert_called_once()
