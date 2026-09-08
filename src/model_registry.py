"""Загрузка зарегистрированных версий без повторного чтения joblib на каждый запрос."""

import math
from hashlib import sha256
from pathlib import Path
from threading import Lock

import joblib
import pandas as pd

from src.config import get_project_root
from src.features import FEATURE_COLUMNS


class PipelinePredictor:
    """Адаптер полного pipeline без внешней импутации и масштабирования."""

    def __init__(self, model, version):
        self.model = model
        self.model_version = version

    def predict(self, features):
        frame = pd.DataFrame([features]).loc[:, FEATURE_COLUMNS]
        predicted = self.model.predict(frame)[0]
        if predicted not in (0, 1):
            raise ValueError("Модель должна возвращать класс 0 или 1")
        prediction = int(predicted)
        probability = None
        if hasattr(self.model, "predict_proba"):
            classes = list(self.model.classes_)
            probability = float(self.model.predict_proba(frame)[0][classes.index(1)])
        if (
            prediction not in (0, 1)
            or probability is None
            or not math.isfinite(probability)
            or not 0 <= probability <= 1
        ):
            raise ValueError(
                "Модель должна возвращать класс 0/1 и вероятность от 0 до 1"
            )
        return {
            "prediction": prediction,
            "probability": probability,
            "label": "detected" if prediction == 1 else "not_detected",
        }


class ModelRegistry:
    def __init__(self) -> None:
        self._cache = {}
        self._lock = Lock()

    def from_record(self, model_record) -> PipelinePredictor:
        """Загружает именно файл выбранной записи model_versions."""
        return self.get_predictor(
            version=model_record.model_version,
            artifact_path=model_record.artifact_path,
            artifact_sha256=model_record.artifact_sha256,
            artifact_format=model_record.artifact_format,
        )

    def get_predictor(
        self,
        *,
        version: str,
        artifact_path: str,
        artifact_sha256: str,
        artifact_format: str = "full-pipeline-v1",
    ) -> PipelinePredictor:
        """Версия связывается с полным pipeline; смена роли не меняет эту связь."""
        if not version or not artifact_sha256:
            raise ValueError("Не заполнены данные зарегистрированной модели")
        if artifact_format != "full-pipeline-v1":
            raise ValueError("Неподдерживаемый формат модели")
        root = get_project_root().resolve()
        path = (root / Path(artifact_path)).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Файл модели должен находиться внутри проекта")
        signature = (str(path), artifact_sha256, artifact_format)
        with self._lock:
            if version in self._cache:
                previous_signature, predictor = self._cache[version]
                if previous_signature != signature:
                    raise ValueError(
                        "Для существующей версии изменены файл или предобработка; зарегистрируйте новую версию"
                    )
                return predictor
            if sha256(path.read_bytes()).hexdigest() != artifact_sha256:
                raise ValueError(
                    "Контрольная сумма файла модели не совпадает с реестром"
                )
            predictor = PipelinePredictor(joblib.load(path), version)
            self._cache[version] = (signature, predictor)
            return predictor
