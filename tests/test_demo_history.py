from collections import Counter
from datetime import date, timedelta

import pytest

from scripts.seed_demo_history import generate_inputs
from src.schemas import DiabetesInput


def reference():
    return [
        {
            "pregnancies": 0,
            "glucose": 120,
            "blood_pressure": 70,
            "skin_thickness": 0,
            "insulin": 0,
            "bmi": 30,
            "diabetes_pedigree_function": 0.5,
            "age": 40,
        }
    ]


def test_demo_spans_sixty_days_evenly_and_has_eighty_percent_agreement():
    end = date(2026, 9, 9)
    rows = generate_inputs(reference(), end=end)
    days = Counter(row["study_date"] for row in rows)
    assert len(rows) == len({row["patient_code"] for row in rows}) == 200
    assert len(days) == 60
    assert min(days) == (end - timedelta(days=59)).isoformat()
    assert max(days) == end.isoformat()
    assert set(days.values()) == {3, 4}
    for recent in (True, False):
        half = [
            row
            for row in rows
            if (date.fromisoformat(row["study_date"]) >= end - timedelta(days=29))
            == recent
        ]
        assert len(half) == 100
        assert sum(row["flip_champion"] for row in half) == 20


def test_demo_inputs_are_reproducible_valid_and_preserve_missing_measurements():
    first = generate_inputs(reference(), end=date(2026, 9, 9))
    assert first == generate_inputs(reference(), end=date(2026, 9, 9))
    assert first != generate_inputs(reference(), end=date(2026, 9, 9), seed=58)
    for row in first:
        payload = {key: value for key, value in row.items() if key != "flip_champion"}
        parsed = DiabetesInput(**payload)
        assert parsed.insulin == parsed.skin_thickness == parsed.pregnancies == 0
        assert 116 <= parsed.glucose <= 124
        assert "outcome" not in row
        assert "prediction" not in row


@pytest.mark.parametrize("count,days", [(0, 60), (1000, 60), (200, 0)])
def test_demo_rejects_invalid_size(count, days):
    with pytest.raises(ValueError):
        generate_inputs(reference(), count=count, days=days)
