import math
from datetime import date
from unittest.mock import MagicMock

import pytest
from prometheus_client import CollectorRegistry, generate_latest
from sqlalchemy import JSON, Column, Float, MetaData, Table, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from src import model_health_exporter
from src.db.models import (
    ModelVersion,
    PredictionFeedback,
    PredictionHistory,
    RawDatasetSample,
    Study,
    TrainingRun,
)
from src.features import FEATURE_COLUMNS
from src.model_health import (
    classification_metrics,
    feature_counts,
    month_window,
    population_stability_index,
    read_model_health,
    target_shift,
    training_row_ids,
)
from src.monitoring import read_quality_snapshot


@pytest.fixture
def quality_db():
    # Настоящие SQL-агрегаты на временной БД. Ограничения PostgreSQL и
    # вычисление train-квантилей проверяются отдельно интеграционными тестами.
    engine = create_engine("sqlite://")
    metadata = MetaData()
    tables = {}
    for model in (
        Study,
        PredictionHistory,
        PredictionFeedback,
        RawDatasetSample,
        ModelVersion,
        TrainingRun,
    ):
        tables[model] = Table(
            model.__tablename__,
            metadata,
            *[
                Column(
                    column.name,
                    JSON() if isinstance(column.type, JSONB) else column.type,
                )
                for column in model.__table__.columns
            ],
        )
    metadata.create_all(engine)
    with Session(engine) as db:
        db.execute(
            tables[Study].insert(),
            [
                {
                    "id": i,
                    "study_date": date(2026, 8, 31) if i == 5 else date(2026, 9, 1),
                    **dict.fromkeys(FEATURE_COLUMNS, 1),
                }
                for i in range(1, 8)
            ],
        )
        db.execute(
            tables[ModelVersion].insert(),
            [
                {
                    "id": i,
                    "model_version": f"v{i}",
                    "model_name": f"model{i}",
                    "family": "decision_tree",
                    "role": "champion" if i == 1 else "challenger",
                    "training_run_id": i,
                }
                for i in (1, 2, 3)
            ],
        )
        db.execute(
            tables[TrainingRun].insert(),
            [{"id": i, "dataset_id": i * 11} for i in (1, 2, 3)],
        )
        db.execute(
            tables[RawDatasetSample].insert(),
            [
                {"dataset_id": 11, "source_study_id": 6},
                {"dataset_id": 22, "source_study_id": 7},
            ],
        )
        db.execute(
            tables[PredictionFeedback].insert(),
            [
                {"study_id": i, "true_label": label}
                for i, label in {1: 1, 2: 0, 3: 1, 5: 1, 6: 1, 7: 0}.items()
            ],
        )
        db.execute(
            tables[PredictionHistory].insert(),
            [
                {
                    "study_id": study_id,
                    "model_version_id": model_id,
                    "prediction": predicted,
                }
                for model_id, predictions in [
                    (1, {1: 1, 2: 1, 4: 1, 5: 1, 6: 1, 7: 0}),
                    (2, {2: 0, 3: 0}),
                ]
                for study_id, predicted in predictions.items()
            ],
        )
        yield db
    engine.dispose()


@pytest.mark.parametrize(
    "model_id,counts",
    [
        (1, {"tp": 1, "tn": 1, "fp": 1, "fn": 0}),
        (2, {"tp": 0, "tn": 1, "fp": 0, "fn": 1}),
        (3, {"tp": 0, "tn": 0, "fp": 0, "fn": 0}),
    ],
)
def test_period_quality_uses_own_predictions_and_own_dataset(
    quality_db, model_id, counts
):
    baseline = {
        "model_id": model_id,
        "version": f"v{model_id}",
        "dataset_id": model_id * 11,
        "role": "challenger",
        "edges": dict.fromkeys(FEATURE_COLUMNS, []),
        "counts": {"samples": 200} | {name + "_bin_0": 200 for name in FEATURE_COLUMNS},
        "positive": 100,
    }
    result = read_model_health(
        quality_db,
        model_id=model_id,
        start=date(2026, 9, 1),
        end=date(2026, 9, 2),
        reference_cache={model_id: baseline},
    )
    assert result["confusion"] == counts
    assert result["evaluated"] == sum(counts.values())
    if model_id == 3:
        assert all(math.isnan(value) for value in result["classification"].values())
    else:
        assert result["accuracy"] == pytest.approx(2 / 3 if model_id == 1 else 1 / 2)


def test_all_time_quality_does_not_wait_for_other_models(quality_db):
    snapshot = read_quality_snapshot(quality_db)
    assert snapshot["cohort"] == 6
    assert [model["counts"] for model in snapshot["models"]] == [
        {"tp": 3, "tn": 1, "fp": 1, "fn": 0},
        {"tp": 0, "tn": 1, "fp": 0, "fn": 1},
        {"tp": 0, "tn": 0, "fp": 0, "fn": 0},
    ]
    # Третья модель ещё не обработала ни одного исследования: это видно
    # в полноте доставки, но не мешает оценить первые две модели.
    assert snapshot["pending"] == 7


def test_month_window_includes_today_and_crosses_year_boundary():
    start, end = month_window(date(2026, 1, 5))
    assert start == date(2025, 12, 7)
    assert end == date(2026, 1, 6)
    assert (end - start).days == 30


def test_small_or_empty_cohort_does_not_claim_no_drift():
    assert math.isnan(population_stability_index([100, 100], [20, 20]))
    assert math.isnan(population_stability_index([20, 20], [100, 100]))
    assert math.isnan(population_stability_index([100, 100], [0, 0]))
    assert math.isnan(target_shift(100, 200, 20, 40))


def test_psi_is_zero_for_same_proportions_with_different_sample_sizes():
    assert population_stability_index([100, 300, 0], [200, 600, 0]) == 0


def test_psi_detects_shift_and_handles_empty_bins():
    value = population_stability_index([100, 0], [0, 100])
    assert math.isfinite(value)
    assert value > 0.25


def test_target_shift_uses_actual_class_proportion_in_percentage_points():
    assert target_shift(40, 100, 120, 200) == pytest.approx(0.2)
    assert target_shift(40, 100, 80, 200) == 0


def test_all_binary_metrics_use_the_same_confusion_matrix():
    metrics = classification_metrics(tp=40, tn=50, fp=5, fn=10)
    assert metrics == pytest.approx(
        {
            "accuracy": 90 / 105,
            "recall": 40 / 50,
            "specificity": 50 / 55,
            "precision": 40 / 45,
            "npv": 50 / 60,
            "f1": 80 / 95,
        }
    )
    assert all(
        math.isnan(value) for value in classification_metrics(0, 0, 0, 0).values()
    )
    negatives = classification_metrics(0, 10, 0, 0)
    assert math.isnan(negatives["recall"])
    assert math.isnan(negatives["f1"])
    assert negatives["npv"] == negatives["specificity"] == 1


def test_reference_is_train_split_of_the_selected_release():
    first = {"dataset": {"row_ids": {"train": [1, 3], "validation": [0], "test": [2]}}}
    second = {"dataset": {"row_ids": {"train": [0, 2], "validation": [1], "test": [3]}}}
    assert training_row_ids(first) == [1, 3]
    assert training_row_ids(second) == [0, 2]
    with pytest.raises(ValueError):
        training_row_ids({"dataset": {}})


def test_histograms_count_every_value_once_and_distinguish_null_from_zero():
    engine = create_engine("sqlite://")
    metadata = MetaData()
    table = Table("sample", metadata, Column("glucose", Float))
    metadata.create_all(engine)
    with engine.begin() as db:
        db.execute(table.insert(), [{"glucose": v} for v in [None, 0, 90, 100, 110]])
        counts = feature_counts(
            db, table, {"glucose": table.c.glucose}, True, {"glucose": [100, 100]}
        )
    assert counts["samples"] == 5
    assert counts["glucose_zeros"] == 1
    assert counts["glucose_missing"] == 1
    bins = [value for key, value in counts.items() if "_bin_" in key]
    assert sum(bins) == 5
    assert bins == [1, 1, 1, 1, 1]


def test_constant_baseline_detects_shift_beyond_its_maximum():
    # Категории: NULL, отсутствующий замер, ниже, равен, выше эталона.
    assert population_stability_index([0, 0, 0, 100, 0], [0, 0, 0, 0, 100]) > 0.25


def test_monthly_exporter_drops_previous_values_after_collection_failure(monkeypatch):
    from src.features import FEATURE_COLUMNS

    snapshot = {
        "start": date(2026, 8, 11),
        "end": date(2026, 9, 10),
        "reference": None,
        "quality": {"samples": 0}
        | {
            name + "_" + kind: 0
            for name in FEATURE_COLUMNS
            for kind in ("zeros", "missing")
        },
    }
    monkeypatch.setattr(model_health_exporter, "get_session_factory", MagicMock())
    monkeypatch.setattr(
        model_health_exporter,
        "read_model_health_snapshots",
        MagicMock(side_effect=[[snapshot], RuntimeError("private credentials")]),
    )
    registry = CollectorRegistry()
    registry.register(model_health_exporter.ModelHealthCollector())
    first = generate_latest(registry).decode()
    assert "diabetes_model_health_studies 0.0" in first
    assert "diabetes_model_health_reference_available 0.0" in first
    assert "diabetes_model_health_feature_psi{" not in first
    assert 'diabetes_model_health_feature_zeros{feature="pregnancies"}' not in first
    failed = generate_latest(registry).decode()
    assert "diabetes_model_health_collection_success 0.0" in failed
    assert "diabetes_model_health_studies" not in failed
    assert "private credentials" not in failed


def test_exporter_keeps_separate_reference_and_values_for_each_model(monkeypatch):
    from src.features import FEATURE_COLUMNS

    snapshots = []
    for version, role, samples, shift in [
        ("release-a-m1", "champion", 536, 0.1),
        ("release-b-m2", "challenger", 800, 0.2),
    ]:
        snapshots.append(
            {
                "start": date(2026, 8, 11),
                "end": date(2026, 9, 10),
                "quality": {"samples": 200}
                | {
                    feature + "_" + kind: 0
                    for feature in FEATURE_COLUMNS
                    for kind in ("zeros", "missing")
                },
                "reference": {
                    "version": version,
                    "role": role,
                    "dataset_id": samples,
                    "samples": samples,
                },
                "current_samples": 200,
                "labeled": 150,
                "positive_rate": 0.4,
                "reference_positive_rate": 0.4 - shift,
                "target_shift": shift,
                "evaluated": 150,
                "accuracy": 0.8,
                "drift": dict.fromkeys(FEATURE_COLUMNS, shift),
                "classification": classification_metrics(40, 80, 10, 20),
                "confusion": {"tp": 40, "tn": 80, "fp": 10, "fn": 20},
            }
        )
    monkeypatch.setattr(model_health_exporter, "get_session_factory", MagicMock())
    monkeypatch.setattr(
        model_health_exporter, "read_model_health_snapshots", lambda db: snapshots
    )
    families = {
        family.name: family
        for family in model_health_exporter.ModelHealthCollector().collect()
    }
    for name, expected in [
        ("reference_samples", [536, 800]),
        ("target_shift", [0.1, 0.2]),
    ]:
        samples = families["diabetes_model_health_" + name].samples
        assert [sample.value for sample in samples] == expected
        assert [sample.labels["version"] for sample in samples] == [
            "release-a-m1",
            "release-b-m2",
        ]
    assert len(families["diabetes_model_health_feature_psi"].samples) == 16
