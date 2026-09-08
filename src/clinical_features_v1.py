"""Immutable version 1 of the clinical input contract used by saved models."""

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, TransformerMixin

FEATURE_COLUMNS = [
    "pregnancies",
    "glucose",
    "blood_pressure",
    "skin_thickness",
    "insulin",
    "bmi",
    "diabetes_pedigree_function",
    "age",
]


TARGET_COLUMN = "outcome"


ZERO_AS_MISSING_COLUMNS = [
    "glucose",
    "blood_pressure",
    "skin_thickness",
    "insulin",
    "bmi",
]


class ClinicalFeatures(TransformerMixin, BaseEstimator):
    """Детерминированная подготовка; статистики здесь не вычисляются."""

    def fit(self, X, y=None):
        self.transform(X)
        self.feature_names_in_ = np.array(FEATURE_COLUMNS, dtype=object)
        self.n_features_in_ = len(FEATURE_COLUMNS)
        return self

    def transform(self, X):
        if (
            not isinstance(X, pd.DataFrame)
            or set(X.columns) != set(FEATURE_COLUMNS)
            or len(X.columns) != len(FEATURE_COLUMNS)
        ):
            raise ValueError("Ожидаются ровно восемь именованных показателей")
        result = X.loc[:, FEATURE_COLUMNS].astype(float).copy()
        if np.isinf(result.to_numpy()).any():
            raise ValueError("Бесконечные значения недопустимы")
        result[ZERO_AS_MISSING_COLUMNS] = result[ZERO_AS_MISSING_COLUMNS].replace(
            0, np.nan
        )
        return result

    def get_feature_names_out(self, input_features=None):
        return np.array(FEATURE_COLUMNS, dtype=object)
