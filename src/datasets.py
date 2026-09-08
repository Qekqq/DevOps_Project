"""Проверка исходных данных без вычисления статистик и изменения значений."""

from hashlib import sha256
from io import BytesIO
from pathlib import Path

import numpy as np
import pandas as pd

from src.features import FEATURE_COLUMNS, RAW_TO_CANONICAL_COLUMNS, TARGET_COLUMN


def read_raw_dataset(path: Path):
    content = Path(path).read_bytes()
    frame = pd.read_csv(BytesIO(content)).rename(columns=RAW_TO_CANONICAL_COLUMNS)
    columns = FEATURE_COLUMNS + [TARGET_COLUMN]
    if len(frame.columns) != len(columns) or set(frame.columns) != set(columns):
        raise ValueError(
            "Ожидаются восемь признаков и outcome без лишних или повторяющихся колонок"
        )
    if frame.empty:
        raise ValueError("Исходный датасет пуст")
    frame = frame.loc[:, columns].apply(pd.to_numeric, errors="raise")
    if np.isinf(frame.to_numpy()).any():
        raise ValueError("Исходный датасет содержит бесконечные значения")
    if (frame[FEATURE_COLUMNS] < 0).any().any():
        raise ValueError("Исходный датасет содержит отрицательные показатели")
    if not frame[TARGET_COLUMN].isin([0, 1]).all():
        raise ValueError("Метка outcome должна быть 0 или 1")
    for column in ["pregnancies", "age"]:
        if (frame[column].dropna() % 1 != 0).any():
            raise ValueError(f"Показатель {column} должен быть целым")
    # Совпадающие строки сохраняются: без идентификатора пациента нельзя
    # заключить, что это повторное наблюдение одного и того же человека.
    return frame, {
        "sha256": sha256(content).hexdigest(),
        "rows": len(frame),
        "duplicate_rows": int(frame.duplicated().sum()),
        "zero_counts": {key: int(value) for key, value in (frame == 0).sum().items()},
    }
