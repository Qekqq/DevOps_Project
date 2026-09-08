from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.register_release import register_release


def manifest():
    return {"release": "r1", "dataset": {}, "provenance": {}, "champion_version": "v1",
        "models": [{"name": "tree", "version": "v1", "artifact_path": "models/v1/model.joblib",
                    "artifact_sha256": "a" * 64, "format": "full-pipeline-v1", "parameters": {},
                    "validation": {"accuracy": .8, "precision": .8, "recall": .8, "f1": .8}}]}


def test_registration_does_not_promote_recommended_champion():
    db = Mock()
    db.execute.return_value.scalar_one_or_none.return_value = None
    register_release(manifest(), db)
    record = db.add.call_args.args[0]
    assert record.role == "challenger"
    assert record.train_medians is None
    assert record.metadata_json["recommended_champion"] is True
    db.commit.assert_not_called()


def test_registration_refuses_reusing_version_for_different_file():
    db = Mock()
    db.execute.return_value.scalar_one_or_none.return_value = SimpleNamespace(artifact_sha256="b" * 64)
    with pytest.raises(ValueError):
        register_release(manifest(), db)
    db.add.assert_not_called()
