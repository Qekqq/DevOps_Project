import pandas as pd
from sklearn.model_selection import train_test_split

from src.config import load_config, get_path
from src.logger import get_logger

from src.features import RAW_TO_CANONICAL_COLUMNS, FEATURE_COLUMNS, TARGET_COLUMN
from src.datasets import read_raw_dataset


class DataPreprocessor:
    """
    Класс для подготовки данных к обучению модели.
    """

    def __init__(self) -> None:
        self.logger = get_logger(self.__class__.__name__)
        self.config = load_config()

        self.raw_data_path = get_path(self.config, "paths", "raw_data_path")

        self.target_column = self.config.get("training", "target_column")
        self.train_size = self.config.getfloat("training", "train_size")
        self.valid_size = self.config.getfloat("training", "valid_size")
        self.test_size = self.config.getfloat("training", "test_size")
        self.random_state = self.config.getint("training", "random_state")


    def load_data(self) -> pd.DataFrame:
        """
        Загружает исходный датасет и приводит названия колонок
        к единому внутреннему формату проекта.
        """
        self.logger.info("Загрузка исходного датасета: %s", self.raw_data_path)

        if not self.raw_data_path.exists():
            raise FileNotFoundError(f"Исходный датасет не найден: {self.raw_data_path}")

        df, _ = read_raw_dataset(self.raw_data_path)

        required_columns = FEATURE_COLUMNS + [TARGET_COLUMN]

        missing_columns = [
            column for column in required_columns
            if column not in df.columns
        ]

        if missing_columns:
            raise ValueError(f"Отсутствуют обязательные столбцы: {missing_columns}")

        df = df[required_columns]

        self.logger.info("Датасет загружен. Размер: %s", df.shape)
        self.logger.info("Столбцы датасета: %s", list(df.columns))

        return df

    def split_data(
        self,
        df: pd.DataFrame,
    ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.Series, pd.Series, pd.Series]:
        """
        Делит данные на train, validation и test выборки со стратификацией по целевой переменной.
        """
        split_sum = self.train_size + self.valid_size + self.test_size

        if round(split_sum, 5) != 1.0:
            raise ValueError("Сумма долей train, validation и test должна быть равна 1")

        self.logger.info(
            "Разбиение данных. Train: %.2f, validation: %.2f, test: %.2f",
            self.train_size,
            self.valid_size,
            self.test_size,
        )

        X = df.drop(columns=[self.target_column])
        y = df[self.target_column]

        X_train_valid, X_test, y_train_valid, y_test = train_test_split(
            X,
            y,
            test_size=self.test_size,
            random_state=self.random_state,
            stratify=y,
        )

        valid_size_from_train_valid = self.valid_size / (self.train_size + self.valid_size)

        X_train, X_valid, y_train, y_valid = train_test_split(
            X_train_valid,
            y_train_valid,
            test_size=valid_size_from_train_valid,
            random_state=self.random_state,
            stratify=y_train_valid,
        )

        self.logger.info("Размер train: %s", X_train.shape)
        self.logger.info("Размер validation: %s", X_valid.shape)
        self.logger.info("Размер test: %s", X_test.shape)

        return X_train, X_valid, X_test, y_train, y_valid, y_test

    def run(self) -> None:
        frame = self.load_data()
        splits = self.split_data(frame)
        print({"rows": len(frame), "train": len(splits[0]), "validation": len(splits[1]), "test": len(splits[2])})


if __name__ == "__main__":
    DataPreprocessor().run()
