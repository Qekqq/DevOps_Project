"""Сущности базы данных. SQL-схема генерируется из этих метаданных."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from src.db.database import Base
from src.features import FEATURE_COLUMNS


def identifier():
    return Column(BigInteger, Identity(), primary_key=True)


def timestamp():
    return Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class User(Base):
    __tablename__ = "users"
    id = identifier()
    username = Column(String(100), nullable=False, unique=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), nullable=False, server_default="user")
    is_active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = timestamp()
    __table_args__ = (CheckConstraint("role IN ('user','admin')"),)


class UserSession(Base):
    __tablename__ = "user_sessions"
    token_hash = Column(String(64), primary_key=True)
    user_id = Column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    password_fingerprint = Column(String(64), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    created_at = timestamp()


class Study(Base):
    __tablename__ = "studies"
    id = identifier()
    patient_code = Column(String(6), nullable=False)
    study_date = Column(Date, nullable=False)
    created_by = Column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"))
    pregnancies = Column(Integer, nullable=False)
    glucose = Column(Numeric, nullable=False)
    blood_pressure = Column(Numeric, nullable=False)
    skin_thickness = Column(Numeric, nullable=False)
    insulin = Column(Numeric, nullable=False)
    bmi = Column(Numeric, nullable=False)
    diabetes_pedigree_function = Column(Numeric, nullable=False)
    age = Column(Integer, nullable=False)
    created_at = timestamp()
    __table_args__ = (
        UniqueConstraint("patient_code", "study_date", name="uq_study_patient_date"),
        CheckConstraint("patient_code ~ '^[A-Z]{3}[0-9]{3}$'"),
        CheckConstraint("pregnancies BETWEEN 0 AND 20"),
        CheckConstraint("glucose BETWEEN 0 AND 600"),
        CheckConstraint("blood_pressure BETWEEN 0 AND 200"),
        CheckConstraint("skin_thickness BETWEEN 0 AND 110"),
        CheckConstraint("insulin BETWEEN 0 AND 1000"),
        CheckConstraint("bmi BETWEEN 0 AND 100"),
        CheckConstraint(
            "diabetes_pedigree_function > 0 AND diabetes_pedigree_function <= 3"
        ),
        CheckConstraint("age > 0 AND age <= 120"),
    )

    @property
    def features(self):
        return {name: float(getattr(self, name)) for name in FEATURE_COLUMNS}

    @features.setter
    def features(self, values):
        for name in FEATURE_COLUMNS:
            setattr(self, name, values[name])


class StudyEdit(Base):
    __tablename__ = "study_edits"
    id = identifier()
    study_id = Column(
        BigInteger, ForeignKey("studies.id", ondelete="RESTRICT"), nullable=False
    )
    changed_by = Column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    features_before = Column(JSONB, nullable=False)
    features_after = Column(JSONB, nullable=False)
    predictions_before = Column(JSONB, nullable=False)
    changed_at = timestamp()


class Dataset(Base):
    __tablename__ = "datasets"
    id = identifier()
    dataset_name = Column(String(100), nullable=False)
    dataset_version = Column(String(64), nullable=False, unique=True)
    source_type = Column(String(20), nullable=False, default="raw")
    source_path = Column(String(1024), nullable=False)
    source_sha256 = Column(String(64), nullable=False)
    row_count = Column(Integer, nullable=False)
    selection_filters = Column(JSONB, nullable=False, default=dict)
    lineage_sha256 = Column(String(64))
    created_at = timestamp()
    __table_args__ = (
        CheckConstraint("row_count > 0"),
        CheckConstraint("source_type IN ('raw','feedback')"),
        CheckConstraint("source_sha256 ~ '^[a-f0-9]{64}$'"),
    )


class RawDatasetSample(Base):
    __tablename__ = "dataset_rows"
    dataset_id = Column(
        BigInteger, ForeignKey("datasets.id", ondelete="RESTRICT"), primary_key=True
    )
    row_number = Column(Integer, primary_key=True)
    source_study_id = Column(BigInteger, ForeignKey("studies.id", ondelete="RESTRICT"))
    features = Column(JSONB, nullable=False)
    outcome = Column(Integer, nullable=False)
    __table_args__ = (
        CheckConstraint("row_number >= 0"),
        CheckConstraint("outcome IN (0,1)"),
    )

    @property
    def sample_values(self):
        return {**self.features, "outcome": self.outcome}

    @sample_values.setter
    def sample_values(self, values):
        self.features = {key: values[key] for key in FEATURE_COLUMNS}
        self.outcome = values["outcome"]


class TrainingRun(Base):
    __tablename__ = "training_runs"
    id = identifier()
    release_id = Column(String(64), nullable=False, unique=True)
    dataset_id = Column(
        BigInteger, ForeignKey("datasets.id", ondelete="RESTRICT"), nullable=False
    )
    configuration = Column(JSONB, nullable=False)
    provenance = Column(JSONB, nullable=False)
    created_at = timestamp()


class ModelVersion(Base):
    __tablename__ = "model_versions"
    id = identifier()
    model_name = Column(String(100), nullable=False)
    model_version = Column(String(50), nullable=False, unique=True)
    family = Column(String(100), nullable=False)
    training_run_id = Column(
        BigInteger, ForeignKey("training_runs.id", ondelete="RESTRICT"), nullable=False
    )
    artifact_path = Column(String(1024), nullable=False)
    artifact_sha256 = Column(String(64), nullable=False)
    artifact_format = Column(String(30), nullable=False)
    params_json = Column(JSONB, nullable=False)
    metrics = Column(JSONB, nullable=False)
    metadata_json = Column(JSONB, nullable=False)
    role = Column(String(20), nullable=False, server_default="challenger")
    created_at = timestamp()
    __table_args__ = (
        CheckConstraint("role IN ('champion','challenger','archived')"),
        CheckConstraint("artifact_sha256 ~ '^[a-f0-9]{64}$'"),
        Index(
            "uq_model_versions_single_champion",
            "role",
            unique=True,
            postgresql_where=text("role = 'champion'"),
        ),
    )


class PredictionHistory(Base):
    __tablename__ = "predictions"
    id = identifier()
    study_id = Column(
        BigInteger, ForeignKey("studies.id", ondelete="RESTRICT"), nullable=False
    )
    model_version_id = Column(
        BigInteger, ForeignKey("model_versions.id", ondelete="RESTRICT"), nullable=False
    )
    prediction = Column(Integer, nullable=False)
    probability = Column(Float, nullable=False)
    role_at_prediction = Column(String(20), nullable=False)
    response_time_ms = Column(Integer)
    created_at = timestamp()
    study = relationship("Study")
    model_version = relationship("ModelVersion")
    __table_args__ = (
        UniqueConstraint(
            "study_id", "model_version_id", name="uq_prediction_study_model"
        ),
        CheckConstraint("prediction IN (0,1)"),
        CheckConstraint("probability BETWEEN 0 AND 1"),
        CheckConstraint("role_at_prediction IN ('champion','challenger')"),
        CheckConstraint("response_time_ms IS NULL OR response_time_ms >= 0"),
    )

    @property
    def label(self):
        return "detected" if self.prediction == 1 else "not_detected"


class ShadowRetry(Base):
    """Незавершённый фоновый расчёт; удаляется после успеха или архивации модели."""

    __tablename__ = "shadow_retries"
    study_id = Column(
        BigInteger, ForeignKey("studies.id", ondelete="RESTRICT"), primary_key=True
    )
    model_version_id = Column(
        BigInteger,
        ForeignKey("model_versions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    attempts = Column(Integer, nullable=False, server_default=text("0"))
    next_attempt_at = timestamp()
    created_at = timestamp()
    __table_args__ = (
        CheckConstraint("attempts >= 0"),
        Index("ix_shadow_retries_due", "next_attempt_at"),
    )


class PredictionFeedback(Base):
    __tablename__ = "feedback"
    id = identifier()
    study_id = Column(
        BigInteger,
        ForeignKey("studies.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    created_by_user_id = Column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"))
    true_label = Column(Integer, nullable=False)
    created_at = timestamp()
    updated_at = timestamp()
    __table_args__ = (CheckConstraint("true_label IN (0,1)"),)


class FeedbackHistory(Base):
    __tablename__ = "feedback_history"
    id = identifier()
    study_id = Column(
        BigInteger, ForeignKey("studies.id", ondelete="RESTRICT"), nullable=False
    )
    old_label = Column(Integer)
    new_label = Column(Integer, nullable=False)
    changed_by = Column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"))
    changed_at = timestamp()
    __table_args__ = (
        CheckConstraint("old_label IS NULL OR old_label IN (0,1)"),
        CheckConstraint("new_label IN (0,1)"),
    )


class ModelRoleHistory(Base):
    __tablename__ = "model_role_history"
    id = identifier()
    model_version_id = Column(
        BigInteger, ForeignKey("model_versions.id", ondelete="RESTRICT"), nullable=False
    )
    old_role = Column(String(20))
    new_role = Column(String(20), nullable=False)
    changed_by = Column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"))
    changed_at = timestamp()
