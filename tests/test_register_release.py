from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.register_release import register_release


def manifest():
    return {"release": "r1", "dataset": {}, "provenance": {}, "champion_version": "v1",
        "models": [{"name": "tree", "version": "v1", "artifact_path": "models/v1/model.joblib",
                    "artifact_sha256": "a" * 64, "format": "full-pipeline-v1", "parameters": {"family": "decision_tree"},
                    "validation": {"accuracy": .8, "precision": .8, "recall": .8, "f1": .8}}]}


def test_registration_does_not_promote_recommended_champion():
    db = Mock()
    db.execute.return_value.scalar_one_or_none.return_value = None
    register_release(manifest(), db, dataset_id=1)
    record = db.add.call_args.args[0]
    assert record.role == "challenger"
    assert record.family == "decision_tree"
    assert record.metadata_json["recommended_champion"] is True
    db.commit.assert_not_called()


def test_registration_refuses_reusing_version_for_different_file():
    db = Mock()
    spec = manifest()
    db.execute.return_value.scalar_one_or_none.side_effect = [
        SimpleNamespace(id=1, dataset_id=1, provenance=spec["provenance"],
                        configuration={"dataset": spec["dataset"], "models": spec["models"],
                                       "champion_version": spec["champion_version"]}),
        SimpleNamespace(artifact_sha256="b" * 64),
    ]
    with pytest.raises(ValueError):
        register_release(manifest(), db, dataset_id=1)
    db.add.assert_not_called()
