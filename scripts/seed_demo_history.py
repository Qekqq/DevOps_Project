"""Демонстрационная история через бизнес-логику API и действующий consumer.

По умолчанию только план. --apply добавляет помеченную синтетическую партию.
Фактические исходы искусственные; метрики не являются независимой оценкой моделей.
"""

import argparse
import json
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256

import numpy as np
from sqlalchemy import func, select, text

from src.db.database import get_session_factory
from src.db.models import (
    Dataset,
    ModelVersion,
    PredictionFeedback,
    PredictionHistory,
    RawDatasetSample,
    Study,
    TrainingRun,
    User,
)
from src.db.repositories import save_prediction_feedback
from src.features import FEATURE_COLUMNS
from src.passwords import hash_password
from src.schemas import DiabetesInput

BATCH = "demo-history-v1"
ACTOR = "demo_history_import"


def generate_inputs(reference, *, count=200, days=60, end=None, seed=57):
    """Стратифицированные даты и небольшие изменения исходных строк train."""
    if not 1 <= count <= 999 or not 2 <= days <= 366 or not reference:
        raise ValueError("Нужны строки эталона, 1–999 исследований и 2–366 дней")
    end = end or datetime.now(timezone.utc).date()
    rng = np.random.default_rng(seed)
    offsets = np.arange(count) * days // count
    rng.shuffle(offsets)
    rows = []
    for index, offset in enumerate(offsets):
        values = dict(reference[int(rng.integers(len(reference)))])
        for feature in FEATURE_COLUMNS:
            value = float(values[feature])
            if feature == "pregnancies":
                values[feature] = int(value)
            elif feature == "age":
                values[feature] = int(np.clip(value + rng.integers(-1, 2), 1, 120))
            elif value == 0:
                values[feature] = 0  # Сохраняем характерную долю отсутствующих замеров.
            else:
                upper = {
                    "glucose": 600,
                    "blood_pressure": 200,
                    "skin_thickness": 110,
                    "insulin": 1000,
                    "bmi": 100,
                    "diabetes_pedigree_function": 3,
                }[feature]
                values[feature] = round(
                    float(np.clip(value * rng.uniform(0.97, 1.03), 0.001, upper)), 3
                )
        record = DiabetesInput(
            patient_code=f"SYN{index:03d}",
            study_date=end - timedelta(days=days - 1 - int(offset)),
            **values,
        ).model_dump(mode="json")
        rows.append(record)
    # Ровно около 20% ошибок в каждой половине, не два разных факта для моделей.
    for half in (0, 1):
        indices = [
            i for i, offset in enumerate(offsets) if int(offset >= days // 2) == half
        ]
        errors = set(
            rng.choice(indices, size=round(len(indices) * 0.2), replace=False).tolist()
        )
        for index in indices:
            rows[index]["flip_champion"] = index in errors
    return rows


def active_versions(db):
    return list(
        db.scalars(
            select(ModelVersion)
            .where(ModelVersion.role.in_(["champion", "challenger"]))
            .order_by(ModelVersion.id)
        )
    )


def make_plan(db, count, days, end, seed):
    models = active_versions(db)
    champion = next((model for model in models if model.role == "champion"), None)
    if champion is None:
        raise ValueError("Нет основной модели")
    run = db.get(TrainingRun, champion.training_run_id)
    train = run.configuration["dataset"]["row_ids"]["train"]
    reference = list(
        db.scalars(
            select(RawDatasetSample.features)
            .where(
                RawDatasetSample.dataset_id == run.dataset_id,
                RawDatasetSample.row_number.in_(train),
            )
            .order_by(RawDatasetSample.row_number)
        )
    )
    if len(reference) != len(train):
        raise ValueError("Неполная обучающая выборка в БД")
    rows = generate_inputs(reference, count=count, days=days, end=end, seed=seed)
    return {
        "batch": BATCH,
        "synthetic": True,
        "status": "prepared",
        "seed": seed,
        "count": count,
        "days": days,
        "end": end.isoformat(),
        "reference_dataset_id": run.dataset_id,
        "champion": champion.model_version,
        "versions": {model.model_version: model.role for model in models},
        "target_generation": "80% совпадений с champion; искусственная обратная связь",
        "rows": rows,
    }


def check_versions(db, plan):
    actual = {model.model_version: model.role for model in active_versions(db)}
    if actual != plan["versions"]:
        raise ValueError("Состав или роли моделей изменились: импорт остановлен")


def summary(plan):
    dates = [row["study_date"] for row in plan["rows"]]
    return {
        "batch": plan["batch"],
        "synthetic": True,
        "count": len(dates),
        "from": min(dates),
        "to": max(dates),
        "models": plan["versions"],
        "planned_champion_errors": sum(row["flip_champion"] for row in plan["rows"]),
    }


def prepare(factory, args):
    with factory() as db:
        existing = db.scalar(select(Dataset).where(Dataset.dataset_version == BATCH))
        if existing is not None:
            plan = existing.selection_filters
            if not plan.get("synthetic") or plan.get("batch") != BATCH:
                raise ValueError("Идентификатор партии занят другим датасетом")
            if (plan["count"], plan["days"], plan["seed"]) != (
                args.count,
                args.days,
                args.seed,
            ):
                raise ValueError("Параметры не совпадают с сохранённой партией")
            if args.end and args.end.isoformat() != plan["end"]:
                raise ValueError("Дата не совпадает с сохранённой партией")
            check_versions(db, plan)
            return plan, existing.id
        end = args.end or datetime.now(timezone.utc).date()
        plan = make_plan(db, args.count, args.days, end, args.seed)
        codes = [row["patient_code"] for row in plan["rows"]]
        if db.scalar(
            select(func.count()).select_from(Study).where(Study.patient_code.in_(codes))
        ):
            raise ValueError(
                "Коды SYN уже используются; существующие исследования не изменены"
            )
        if not args.apply:
            return plan, None
        actor = db.scalar(select(User).where(User.username == ACTOR))
        if actor is not None:
            raise ValueError("Имя служебного автора уже занято без записи партии")
        db.add(
            User(
                username=ACTOR,
                password_hash=hash_password(secrets.token_urlsafe(32)),
                role="user",
                is_active=False,
            )
        )
        dataset = Dataset(
            dataset_name="Демонстрационная история: искусственные исходы",
            dataset_version=BATCH,
            source_type="raw",
            source_path="synthetic://" + BATCH,
            source_sha256=sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest(),
            row_count=len(plan["rows"]),
            selection_filters=plan,
        )
        db.add(dataset)
        db.commit()
        return plan, dataset.id


def import_predictions(factory, plan):
    from src.app import predict_diabetes

    for index, row in enumerate(plan["rows"]):
        with factory() as db:
            check_versions(db, plan)
            actor = db.scalar(select(User).where(User.username == ACTOR))
            if actor is None or actor.is_active:
                raise ValueError("Некорректный служебный автор демонстрационных данных")
            study = db.scalar(
                select(Study).where(
                    Study.patient_code == row["patient_code"],
                    Study.study_date == date.fromisoformat(row["study_date"]),
                )
            )
            if study is not None and study.created_by != actor.id:
                raise ValueError("Исследование принадлежит другому автору")
        payload = {key: value for key, value in row.items() if key != "flip_champion"}
        # Та же бизнес-функция, что у POST /predict. Она считает champion и
        # ждёт приёма сообщения Kafka; работающий consumer считает challenger.
        predict_diabetes(DiabetesInput(**payload), actor)
        if (index + 1) % 25 == 0:
            print(f"Передано на расчёт: {index + 1}/{len(plan['rows'])}", flush=True)


def wait_predictions(factory, plan, timeout=180):
    deadline = time.monotonic() + timeout
    codes = [row["patient_code"] for row in plan["rows"]]
    expected = len(codes) * len(plan["versions"])
    while time.monotonic() < deadline:
        with factory() as db:
            check_versions(db, plan)
            count = db.scalar(
                select(func.count())
                .select_from(PredictionHistory)
                .join(Study, Study.id == PredictionHistory.study_id)
                .join(
                    ModelVersion, ModelVersion.id == PredictionHistory.model_version_id
                )
                .where(
                    Study.patient_code.in_(codes),
                    ModelVersion.model_version.in_(plan["versions"]),
                )
            )
        if count == expected:
            return
        time.sleep(2)
    raise RuntimeError(
        "Не все модели рассчитаны; повторите ту же команду после проверки consumer"
    )


def save_demo_outcomes(factory, plan, dataset_id):
    with factory() as db:
        check_versions(db, plan)
        actor = db.scalar(select(User).where(User.username == ACTOR))
        champion = db.scalar(
            select(ModelVersion).where(ModelVersion.model_version == plan["champion"])
        )
        for index, row in enumerate(plan["rows"]):
            study = db.scalar(
                select(Study).where(
                    Study.patient_code == row["patient_code"],
                    Study.study_date == date.fromisoformat(row["study_date"]),
                )
            )
            if study.created_by != actor.id:
                raise ValueError("Исследование принадлежит другому автору")
            prediction = db.scalar(
                select(PredictionHistory).where(
                    PredictionHistory.study_id == study.id,
                    PredictionHistory.model_version_id == champion.id,
                )
            )
            label = int(prediction.prediction) ^ int(row["flip_champion"])
            saved = db.scalar(
                select(PredictionFeedback).where(
                    PredictionFeedback.study_id == study.id
                )
            )
            if saved is not None and (
                saved.created_by_user_id != actor.id or saved.true_label != label
            ):
                raise ValueError(
                    "Обратная связь была изменена вручную; импорт её не перезаписывает"
                )
            save_prediction_feedback(
                db, study_id=study.id, true_label=label, created_by_user_id=actor.id
            )
            entry = db.get(RawDatasetSample, (dataset_id, index))
            if entry is None:
                db.add(
                    RawDatasetSample(
                        dataset_id=dataset_id,
                        row_number=index,
                        source_study_id=study.id,
                        features=study.features,
                        outcome=label,
                    )
                )
            elif (
                entry.source_study_id != study.id
                or entry.outcome != label
                or entry.features != study.features
            ):
                raise ValueError(
                    "Сохранённые сведения о демонстрационной партии не совпадают"
                )
        # datasets неизменяема: план остаётся исходным. Завершённость проверяем
        # по наличию всех dataset_rows, прогнозов и обратной связи, без UPDATE.
        db.commit()


def report(factory, plan):
    with factory() as db:
        codes = [row["patient_code"] for row in plan["rows"]]
        rows = db.execute(
            select(
                ModelVersion.model_version,
                func.count(),
                func.count().filter(
                    PredictionHistory.prediction == PredictionFeedback.true_label
                ),
            )
            .select_from(PredictionHistory)
            .join(Study, Study.id == PredictionHistory.study_id)
            .join(PredictionFeedback, PredictionFeedback.study_id == Study.id)
            .join(ModelVersion, ModelVersion.id == PredictionHistory.model_version_id)
            .where(
                Study.patient_code.in_(codes),
                ModelVersion.model_version.in_(plan["versions"]),
            )
            .group_by(ModelVersion.model_version)
        ).all()
        return {
            **summary(plan),
            "results": [
                {
                    "version": version,
                    "predictions": count,
                    "correct": correct,
                    "accuracy": correct / count,
                }
                for version, count, correct in rows
            ],
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--seed", type=int, default=57)
    parser.add_argument("--end", type=date.fromisoformat)
    args = parser.parse_args()
    factory = get_session_factory()
    # Один импортёр партии, без блокировки обычных запросов приложения.
    with factory() as lock:
        if not lock.scalar(
            text("SELECT pg_try_advisory_lock(hashtextextended('demo-history-v1', 0))")
        ):
            raise RuntimeError("Импорт этой партии уже выполняется")
        try:
            plan, dataset_id = prepare(factory, args)
            print(json.dumps(summary(plan), ensure_ascii=False), flush=True)
            if args.apply:
                import_predictions(factory, plan)
                wait_predictions(factory, plan)
                save_demo_outcomes(factory, plan, dataset_id)
                print(json.dumps(report(factory, plan), ensure_ascii=False), flush=True)
        finally:
            lock.execute(
                text(
                    "SELECT pg_advisory_unlock(hashtextextended('demo-history-v1', 0))"
                )
            )


if __name__ == "__main__":
    main()
