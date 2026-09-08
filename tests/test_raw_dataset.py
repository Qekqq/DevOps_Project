from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from src.config import get_project_root
from src.datasets import read_raw_dataset
from src.db.load_raw_dataset import import_raw_dataset


def test_raw_audit_preserves_zeros_and_rows():
    frame, audit = read_raw_dataset(get_project_root() / "data/raw/diabetes.csv")
    assert len(frame) == audit["rows"] == 768
    assert audit["zero_counts"]["insulin"] == 374
    assert (frame["insulin"] == 0).sum() == 374


def test_repeated_import_does_not_insert_rows():
    db = Mock()
    existing = SimpleNamespace(id=1, row_count=768)
    db.execute.return_value.scalar_one_or_none.return_value = existing
    assert import_raw_dataset(db, get_project_root() / "data/raw/diabetes.csv") is existing
    db.add.assert_not_called()
    db.add_all.assert_not_called()


def test_invalid_target_rejected(tmp_path):
    frame, _ = read_raw_dataset(get_project_root() / "data/raw/diabetes.csv")
    frame.loc[0, "outcome"] = 2
    path = tmp_path / "invalid.csv"
    frame.to_csv(path, index=False)
    with pytest.raises(ValueError):
        read_raw_dataset(path)
