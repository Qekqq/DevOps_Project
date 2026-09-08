"""Полные обучаемые pipeline: один контракт входа для обучения и API."""
import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.tree import DecisionTreeClassifier

from src.features import FEATURE_COLUMNS, ZERO_AS_MISSING_COLUMNS


class ClinicalFeatures(TransformerMixin, BaseEstimator):
    """Детерминированная подготовка; статистики здесь не вычисляются."""

    def fit(self, X, y=None):
        self.transform(X)
        self.feature_names_in_ = np.array(FEATURE_COLUMNS, dtype=object)
        self.n_features_in_ = len(FEATURE_COLUMNS)
        return self

    def transform(self, X):
        if not isinstance(X, pd.DataFrame) or set(X.columns) != set(FEATURE_COLUMNS) or len(X.columns) != len(FEATURE_COLUMNS):
            raise ValueError("Ожидаются ровно восемь именованных показателей")
        result = X.loc[:, FEATURE_COLUMNS].astype(float).copy()
        if np.isinf(result.to_numpy()).any():
            raise ValueError("Бесконечные значения недопустимы")
        result[ZERO_AS_MISSING_COLUMNS] = result[ZERO_AS_MISSING_COLUMNS].replace(0, np.nan)
        return result

    def get_feature_names_out(self, input_features=None):
        return np.array(FEATURE_COLUMNS, dtype=object)


def build_pipeline(
    family: str,
    *,
    random_state: int,
    parameters: dict,
    preprocessing: dict | None = None,
) -> Pipeline:
    preprocessing = preprocessing or {}
    strategy = preprocessing.get("imputation", "median")
    if strategy not in {"median", "mean"}:
        raise ValueError("Способ заполнения должен быть median или mean")
    overrides = preprocessing.get("imputation_by_feature", {})
    if not isinstance(overrides, dict):
        raise ValueError("imputation_by_feature должен содержать настройки по названиям признаков")
    unknown = set(overrides) - set(FEATURE_COLUMNS)
    if unknown:
        raise ValueError(f"Неизвестные признаки в настройках заполнения: {sorted(unknown)}")
    if any(value not in {"median", "mean"} for value in overrides.values()):
        raise ValueError("Способ заполнения каждого признака должен быть median или mean")
    if overrides:
        # Порядок столбцов одинаков при обучении и прогнозе независимо от порядка настроек.
        imputer = ColumnTransformer([
            (
                column,
                SimpleImputer(
                    strategy=overrides.get(column, strategy),
                    keep_empty_features=True,
                ),
                [column],
            )
            for column in FEATURE_COLUMNS
        ], verbose_feature_names_out=False)
    else:
        imputer = SimpleImputer(strategy=strategy, keep_empty_features=True)
    steps = [
        ("clinical_features", ClinicalFeatures()),
        ("imputer", imputer),
    ]
    if family == "logistic_regression":
        classifier = LogisticRegression(random_state=random_state, **parameters)
    elif family == "decision_tree":
        classifier = DecisionTreeClassifier(random_state=random_state, **parameters)
    else:
        raise ValueError(f"Неизвестное семейство моделей: {family}")
    if preprocessing.get("scale", family == "logistic_regression"):
        steps.append(("scaler", StandardScaler()))
    return Pipeline([*steps, ("classifier", classifier)])
