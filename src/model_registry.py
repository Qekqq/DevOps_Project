"""Загрузка зарегистрированных версий без повторного чтения joblib на каждый запрос."""
from hashlib import sha256
import json
from pathlib import Path
from threading import Lock

from src.config import get_project_root
from src.predict import DiabetesPredictor


class ModelRegistry:
    def __init__(self) -> None:
        self._cache = {}
        self._lock = Lock()

    def from_record(self, model_record) -> DiabetesPredictor:
        """Загружает именно файл выбранной записи model_versions."""
        return self.get_predictor(
            version=model_record.model_version,
            artifact_path=model_record.artifact_path,
            artifact_sha256=model_record.artifact_sha256,
            train_medians=model_record.train_medians,
        )

    def get_predictor(
        self, *, version: str, artifact_path: str,
        artifact_sha256: str, train_medians: dict[str, float],
    ) -> DiabetesPredictor:
        """Версия связывается с файлом и медианами; смена роли не меняет эту связь."""
        if not version or not artifact_sha256 or not train_medians:
            raise ValueError("Не заполнены данные зарегистрированной модели")
        root = get_project_root().resolve()
        path = (root / Path(artifact_path)).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Файл модели должен находиться внутри проекта")
        signature = (str(path), artifact_sha256, json.dumps(train_medians, sort_keys=True))
        with self._lock:
            if version in self._cache:
                previous_signature, predictor = self._cache[version]
                if previous_signature != signature:
                    raise ValueError("Для существующей версии изменены файл или предобработка; зарегистрируйте новую версию")
                return predictor
            if sha256(path.read_bytes()).hexdigest() != artifact_sha256:
                raise ValueError("Контрольная сумма файла модели не совпадает с реестром")
            predictor = DiabetesPredictor(
                model_path=path, model_version=version, train_medians=train_medians,
            )
            self._cache[version] = (signature, predictor)
            return predictor
