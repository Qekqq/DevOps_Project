from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    text,
    func,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.orm import relationship

from src.db.database import Base


user_role_enum = ENUM(
    "user",
    "admin",
    name="user_role",
    create_type=False,
)

prediction_label_enum = ENUM(
    "detected",
    "not_detected",
    name="prediction_label",
    create_type=False,
)

request_source_enum = ENUM(
    "api",
    "frontend",
    "test",
    name="request_source",
    create_type=False,
)

dataset_split_enum = ENUM(
    "train",
    "valid",
    "test",
    name="dataset_split",
    create_type=False,
)

model_role_enum = ENUM(
    "champion",
    "challenger",
    "archived",
    name="model_role",
    create_type=False,
)


class User(Base):
    __tablename__ = "users"

    id = Column(BigInteger, Identity(always=True), primary_key=True)
    username = Column(String(100), nullable=False, unique=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(user_role_enum, nullable=False, server_default=text("'user'"))
    is_active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    predictions = relationship("PredictionHistory", back_populates="user")
    feedback_items = relationship("PredictionFeedback", back_populates="created_by_user")


class Patient(Base):
    __tablename__ = "patients"

    id = Column(BigInteger, Identity(always=True), primary_key=True)
    patient_code = Column(String(100), nullable=False, unique=True)
    is_active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    predictions = relationship("PredictionHistory", back_populates="patient")


class Dataset(Base):
    __tablename__ = "datasets"

    __table_args__ = (
        UniqueConstraint("dataset_name", "dataset_version", name="uq_datasets_name_version"),
    )

    id = Column(BigInteger, Identity(always=True), primary_key=True)
    dataset_name = Column(String(100), nullable=False)
    dataset_version = Column(String(50), nullable=False)
    source_path = Column(String(255))
    source_sha256 = Column(String(64), unique=True)
    row_count = Column(Integer)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    samples = relationship("DatasetSample", back_populates="dataset")
    model_versions = relationship("ModelVersion", back_populates="trained_on_dataset")


class RawDatasetSample(Base):
    __tablename__ = "raw_dataset_samples"
    dataset_id = Column(BigInteger, ForeignKey("datasets.id", ondelete="RESTRICT"), primary_key=True)
    row_number = Column(Integer, primary_key=True)
    sample_values = Column(JSONB, nullable=False)


class DatasetSample(Base):
    __tablename__ = "dataset_samples"

    id = Column(BigInteger, Identity(always=True), primary_key=True)

    dataset_id = Column(
        BigInteger,
        ForeignKey("datasets.id", ondelete="CASCADE"),
        nullable=False,
    )

    split = Column(dataset_split_enum, nullable=False)

    pregnancies = Column(Integer, nullable=False)
    glucose = Column(Numeric(6, 3), nullable=False)
    blood_pressure = Column(Numeric(6, 3), nullable=False)
    skin_thickness = Column(Numeric(6, 3), nullable=False)
    insulin = Column(Numeric(7, 3), nullable=False)
    bmi = Column(Numeric(6, 3), nullable=False)
    diabetes_pedigree_function = Column(Numeric(4, 3), nullable=False)
    age = Column(Integer, nullable=False)

    outcome = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    dataset = relationship("Dataset", back_populates="samples")


class ModelVersion(Base):
    __tablename__ = "model_versions"

    id = Column(BigInteger, Identity(always=True), primary_key=True)

    model_name = Column(String(100), nullable=False)
    model_version = Column(String(50), nullable=False, unique=True)
    artifact_path = Column(String(255), nullable=False)
    artifact_sha256 = Column(String(64), nullable=False)
    train_medians = Column(JSONB)
    artifact_format = Column(String(30), nullable=False, server_default=text("'legacy-v1'"))
    metadata_json = Column(JSONB)
    preprocessing_version = Column(String(50))

    trained_on_dataset_id = Column(
        BigInteger,
        ForeignKey("datasets.id", ondelete="SET NULL"),
    )

    accuracy_score = Column(Numeric(18, 16))
    precision_score = Column(Numeric(18, 16))
    recall_score = Column(Numeric(18, 16))
    f1_score = Column(Numeric(18, 16))

    params_json = Column(JSONB)

    traffic_weight = Column(Integer, nullable=False, server_default=text("100"))
    role = Column(model_role_enum, nullable=False, server_default=text("'challenger'"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    trained_on_dataset = relationship("Dataset", back_populates="model_versions")
    predictions = relationship("PredictionHistory", back_populates="model_version")


class Study(Base):
    __tablename__ = "studies"

    id = Column(BigInteger, Identity(always=True), primary_key=True)
    patient_code = Column(String(6), nullable=False)
    study_date = Column(Date, nullable=False)
    features = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        UniqueConstraint("patient_code", "study_date", name="uq_studies_patient_date"),
        CheckConstraint("patient_code ~ '^[A-Z]{3}[0-9]{3}$'", name="chk_studies_patient_code"),
    )
    predictions = relationship("PredictionHistory", back_populates="study")
    feedback = relationship("PredictionFeedback", back_populates="study", uselist=False)


class PredictionHistory(Base):
    __tablename__ = "prediction_history"

    study_id = Column(BigInteger, ForeignKey("studies.id", ondelete="RESTRICT"), nullable=False)
    study = relationship("Study", back_populates="predictions")

    id = Column(BigInteger, Identity(always=True), primary_key=True)

    user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
    )

    patient_id = Column(
        BigInteger,
        ForeignKey("patients.id", ondelete="SET NULL"),
    )

    model_version_id = Column(
        BigInteger,
        ForeignKey("model_versions.id", ondelete="SET NULL"),
    )

    patient_code_snapshot = Column(String(100))
    model_version_snapshot = Column(String(50), nullable=False)
    study_date = Column(Date, nullable=False)
    inference_payload = Column(JSONB)

    __table_args__ = (
        Index(
            "uq_prediction_history_study_model",
            study_id,
            model_version_snapshot,
            unique=True,
        ),
    )

    pregnancies = Column(Integer, nullable=False)
    glucose = Column(Numeric(6, 3), nullable=False)
    blood_pressure = Column(Numeric(6, 3), nullable=False)
    skin_thickness = Column(Numeric(6, 3), nullable=False)
    insulin = Column(Numeric(7, 3), nullable=False)
    bmi = Column(Numeric(6, 3), nullable=False)
    diabetes_pedigree_function = Column(Numeric(4, 3), nullable=False)
    age = Column(Integer, nullable=False)

    prediction = Column(Integer, nullable=False)
    probability = Column(Numeric(6, 5))
    label = Column(prediction_label_enum, nullable=False)

    request_source = Column(
        request_source_enum,
        nullable=False,
        server_default=text("'frontend'"),
    )
    response_time_ms = Column(Integer)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    user = relationship("User", back_populates="predictions")
    patient = relationship("Patient", back_populates="predictions")
    model_version = relationship("ModelVersion", back_populates="predictions")

class PredictionFeedback(Base):
    __tablename__ = "prediction_feedback"

    __table_args__ = (
        CheckConstraint("true_label IN (0, 1)", name="chk_feedback_true_label_value"),
    )

    id = Column(BigInteger, Identity(always=True), primary_key=True)

    study_id = Column(
        BigInteger,
        ForeignKey("studies.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )

    created_by_user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
    )

    true_label = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    study = relationship("Study", back_populates="feedback")
    created_by_user = relationship("User", back_populates="feedback_items")
