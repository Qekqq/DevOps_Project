from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from src.db.models import ModelVersion
from src.kafka import retries


@pytest.mark.parametrize("fails", [False, True])
def test_retry_retains_failure_and_removes_success_using_current_features(
    monkeypatch, fails
):
    db = MagicMock()
    db.__enter__.return_value = db
    task = SimpleNamespace(attempts=50)
    db.scalar.return_value = task
    model = SimpleNamespace(role="challenger", model_version="v2")
    study = SimpleNamespace(
        patient_code="RET001", study_date=date(2026, 9, 10), features={"glucose": 123}
    )
    db.get.side_effect = lambda table, key: model if table is ModelVersion else study
    monkeypatch.setattr(retries, "get_session_factory", lambda: lambda: db)
    predict = Mock(side_effect=RuntimeError("broken") if fails else None)
    monkeypatch.setattr(retries, "predict_challengers", predict)
    started = datetime.now(timezone.utc)
    retries.retry_task(1, 2)
    assert predict.call_args.args[0]["features"] == study.features
    assert predict.call_args.kwargs == {"only_versions": {"v2"}}
    db.commit.assert_called_once()
    if fails:
        db.delete.assert_not_called()
        assert task.attempts == 51
        assert 1799 <= (task.next_attempt_at - started).total_seconds() <= 1802
    else:
        db.delete.assert_called_once_with(task)


@pytest.mark.parametrize("role", ["champion", "archived"])
def test_retry_does_not_run_inactive_challenger(monkeypatch, role):
    db = MagicMock()
    db.__enter__.return_value = db
    db.get.return_value = SimpleNamespace(role=role)
    monkeypatch.setattr(retries, "get_session_factory", lambda: lambda: db)
    predict = Mock()
    monkeypatch.setattr(retries, "predict_challengers", predict)
    retries.retry_task(1, 2)
    predict.assert_not_called()
    db.delete.assert_called_once_with(db.scalar.return_value)
    db.commit.assert_called_once()


def test_locked_or_not_due_task_is_skipped(monkeypatch):
    db = MagicMock()
    db.__enter__.return_value = db
    db.scalar.return_value = None
    monkeypatch.setattr(retries, "get_session_factory", lambda: lambda: db)
    predict = Mock()
    monkeypatch.setattr(retries, "predict_challengers", predict)
    retries.retry_task(1, 2)
    predict.assert_not_called()
    db.commit.assert_not_called()


def test_one_failed_retry_does_not_stop_other_due_tasks(monkeypatch):
    db = MagicMock()
    db.__enter__.return_value = db
    db.execute.return_value.all.return_value = [(1, 2), (3, 4)]
    monkeypatch.setattr(retries, "get_session_factory", lambda: lambda: db)
    retry = Mock(side_effect=[RuntimeError("failed"), None])
    monkeypatch.setattr(retries, "retry_task", retry)
    retries.retry_pending()
    assert [call.args for call in retry.call_args_list] == [(1, 2), (3, 4)]
