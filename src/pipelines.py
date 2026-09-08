"""Полные обучаемые pipeline: один контракт входа для обучения и API."""

from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.tree import DecisionTreeClassifier

from src.clinical_features_v1 import ClinicalFeatures
from src.features import FEATURE_COLUMNS


def build_pipeline(
    family: str,
    *,
    random_state: int,
    parameters: dict,
    preprocessing: dict | None = None,
) -> Pipeline:
    preprocessing = preprocessing or {}
    unknown_options = set(preprocessing) - {
        "imputation",
        "imputation_by_feature",
        "scale",
    }
    if unknown_options:
        raise ValueError(
            f"Неизвестные настройки предобработки: {sorted(unknown_options)}"
        )
    if "scale" in preprocessing and type(preprocessing["scale"]) is not bool:
        raise ValueError("Настройка scale должна быть true или false")
    strategy = preprocessing.get("imputation", "median")
    if strategy not in {"median", "mean"}:
        raise ValueError("Способ заполнения должен быть median или mean")
    overrides = preprocessing.get("imputation_by_feature", {})
    if not isinstance(overrides, dict):
        raise ValueError(
            "imputation_by_feature должен содержать настройки по названиям признаков"
        )
    unknown = set(overrides) - set(FEATURE_COLUMNS)
    if unknown:
        raise ValueError(
            f"Неизвестные признаки в настройках заполнения: {sorted(unknown)}"
        )
    if any(value not in {"median", "mean"} for value in overrides.values()):
        raise ValueError(
            "Способ заполнения каждого признака должен быть median или mean"
        )
    if overrides:
        # Порядок столбцов одинаков при обучении и прогнозе независимо от порядка настроек.
        imputer = ColumnTransformer(
            [
                (
                    column,
                    SimpleImputer(
                        strategy=overrides.get(column, strategy),
                        keep_empty_features=True,
                    ),
                    [column],
                )
                for column in FEATURE_COLUMNS
            ],
            verbose_feature_names_out=False,
        )
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
