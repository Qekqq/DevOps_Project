"""Локальная проверка champion из манифеста; рабочий API выбирает её из БД."""

import json

from src.config import get_path, load_config
from src.model_registry import ModelRegistry


def load_release_models():
    path = get_path(load_config(), "paths", "release_manifest")
    return json.loads(path.read_text(encoding="utf-8"))


def DiabetesPredictor():
    manifest = load_release_models()
    record = next(
        record
        for record in manifest["models"]
        if record["version"] == manifest["champion_version"]
    )
    return ModelRegistry().get_predictor(
        version=record["version"],
        artifact_path=record["artifact_path"],
        artifact_sha256=record["artifact_sha256"],
        artifact_format=record["format"],
    )
