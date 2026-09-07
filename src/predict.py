import joblib
import pandas as pd
from pathlib import Path

from src.config import load_config, get_path, get_zero_as_missing_columns
from src.logger import get_logger
from src.features import FEATURE_COLUMNS, RAW_TO_CANONICAL_COLUMNS


class DiabetesPredictor:
    """
    Класс для загрузки обученной модели и выполнения предсказаний.
    """

    def __init__(
        self, *, model_path: Path | None = None,
        model_version: str | None = None,
        train_medians: dict[str, float] | None = None,
    ) -> None:
        self.logger = get_logger(self.__class__.__name__)
        self.config = load_config()
        self.model_version = model_version if model_version is not None else self.config.get("model", "model_version")

        self.model_path = Path(model_path) if model_path is not None else get_path(self.config, "paths", "best_model_path")
        self.train_data_path = get_path(self.config, "paths", "train_data_path")

        self.target_column = self.config.get("training", "target_column")
        self.zero_as_missing_columns = get_zero_as_missing_columns(self.config)

        self.model = self.load_model()
        self.model_feature_columns = self.get_feature_columns()
        self.feature_columns = [
            RAW_TO_CANONICAL_COLUMNS.get(column, column)
            for column in self.model_feature_columns
        ]
        if len(self.feature_columns) != len(FEATURE_COLUMNS) or set(self.feature_columns) != set(FEATURE_COLUMNS):
            raise ValueError("Набор признаков модели не соответствует восьми показателям API")
        self.train_medians = dict(train_medians) if train_medians is not None else self.get_train_medians()
        if any(column not in self.train_medians for column in self.zero_as_missing_columns):
            raise ValueError("Не заданы медианы предобработки для всех необходимых признаков")

    def load_model(self):
        """
        Загружает финальную обученную модель.
        """
        self.logger.info("Loading model from %s", self.model_path)

        if not self.model_path.exists():
            raise FileNotFoundError(f"Model file was not found: {self.model_path}")

        model = joblib.load(self.model_path)

        self.logger.info("Model loaded successfully.")
        return model

    def get_feature_columns(self) -> list[str]:
        """
        Получает список признаков, которые нужны модели для предсказания.
        """
        if hasattr(self.model, "feature_names_in_"):
            feature_columns = list(self.model.feature_names_in_)
            self.logger.info("Feature columns loaded from model: %s", feature_columns)
            return feature_columns

        train_data = pd.read_csv(self.train_data_path)
        feature_columns = [
            column for column in train_data.columns
            if column != self.target_column
        ]

        self.logger.info("Feature columns loaded from train data: %s", feature_columns)
        return feature_columns

    def get_train_medians(self) -> dict[str, float]:
        """
        Рассчитывает медианы train-выборки для колонок, где нули считаются пропусками.
        """
        train_data = pd.read_csv(self.train_data_path)

        train_medians = (
            train_data[self.zero_as_missing_columns]
            .median()
            .to_dict()
        )

        self.logger.info("Train medians loaded: %s", train_medians)
        return train_medians

    def validate_input(self, input_data: dict) -> None:
        """
        Проверяет, что во входных данных есть все необходимые признаки.
        """
        missing_columns = [
            column for column in self.feature_columns
            if column not in input_data
        ]

        if missing_columns:
            raise ValueError(f"Missing input columns: {missing_columns}")

    def prepare_input(self, input_data: dict) -> pd.DataFrame:
        """
        Подготавливает входные данные к предсказанию.
        """
        self.validate_input(input_data)

        input_df = pd.DataFrame([input_data])
        input_df = input_df[self.feature_columns]

        for column in self.zero_as_missing_columns:
            if column in input_df.columns:
                input_df[column] = input_df[column].replace(
                    0,
                    self.train_medians[column],
                )

        # Медианы применяются к именам API, затем восстанавливаются имена
        # и порядок столбцов, с которыми конкретная модель была обучена.
        input_df.columns = self.model_feature_columns
        return input_df

    def predict(self, input_data: dict) -> dict:
        """
        Выполняет предсказание для одного объекта.
        """
        self.logger.info("Prediction started.")

        input_df = self.prepare_input(input_data)

        prediction = int(self.model.predict(input_df)[0])

        probability = None
        if hasattr(self.model, "predict_proba"):
            probability = float(self.model.predict_proba(input_df)[0][1])

        result = {
            "prediction": prediction,
            "probability": probability,
            "label": "detected" if prediction == 1 else "not_detected",
        }

        self.logger.info("Prediction result: %s", result)

        return result


if __name__ == "__main__":
    example_input = {
        "pregnancies": 6,
        "glucose": 148,
        "blood_pressure": 72,
        "skin_thickness": 35,
        "insulin": 0,
        "bmi": 33.6,
        "diabetes_pedigree_function": 0.627,
        "age": 50,
    }

    predictor = DiabetesPredictor()
    result = predictor.predict(example_input)

    print(result)
