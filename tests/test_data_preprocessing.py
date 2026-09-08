import pandas as pd

from src.data_preprocessing import DataPreprocessor


def test_splits_are_disjoint_repeatable_and_preserve_original_values():
    processor = DataPreprocessor()
    raw = processor.load_data()
    first = processor.split_data(raw)
    second = processor.split_data(raw)
    parts = [set(part.index) for part in first[:3]]
    assert (
        not parts[0] & parts[1] and not parts[0] & parts[2] and not parts[1] & parts[2]
    )
    assert set.union(*parts) == set(raw.index)
    for X, y, again in zip(first[:3], first[3:], second[:3]):
        pd.testing.assert_frame_equal(X, again)
        pd.testing.assert_frame_equal(X, raw.drop(columns="outcome").loc[X.index])
        pd.testing.assert_series_equal(y, raw.loc[y.index, "outcome"])
    assert sum((X["insulin"] == 0).sum() for X in first[:3]) == 374
