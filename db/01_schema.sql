-- ============================================================
-- 01_schema.sql
-- PostgreSQL schema for DevOps HW 2 / Diabetes Predict
--
-- Назначение:
--   - пользователи с ролями user/admin;
--   - пациенты;
--   - датасеты и строки датасетов;
--   - версии ML-моделей;
--   - история предсказаний;
--   - обратная связь по предсказаниям.
--
-- Важно:
--   В медицинских признаках glucose, blood_pressure,
--   skin_thickness, insulin, bmi значение 0 допускается
--   как техническое значение "не измерено / пропуск".
--   Отрицательные значения запрещены.
-- ============================================================


-- ============================================================
-- ENUM-типы
-- ============================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'user_role') THEN
        CREATE TYPE user_role AS ENUM ('user', 'admin');
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'prediction_label') THEN
        CREATE TYPE prediction_label AS ENUM (
            'detected',
            'not_detected'
        );
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'request_source') THEN
        CREATE TYPE request_source AS ENUM (
            'api',
            'frontend',
            'test'
        );
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'dataset_split') THEN
        CREATE TYPE dataset_split AS ENUM (
            'train',
            'valid',
            'test'
        );
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'model_role') THEN
        CREATE TYPE model_role AS ENUM (
            'champion',
            'challenger',
            'archived'
        );
    END IF;
END $$;

COMMENT ON TYPE user_role IS 'Роль пользователя в системе: обычный пользователь или администратор.';
COMMENT ON TYPE prediction_label IS 'Метка результата предсказания модели.';
COMMENT ON TYPE request_source IS 'Источник запроса на выполнение предсказания.';
COMMENT ON TYPE dataset_split IS 'Сплит датасета: обучение, валидация или тест.';
COMMENT ON TYPE model_role IS 'Роль версии модели: champion (боевая), challenger (кандидат), archived (выведена из эксплуатации).';


-- ============================================================
-- users
-- Пользователи системы.
-- Регистрации нет: пользователей заводит администратор.
-- ============================================================

CREATE TABLE IF NOT EXISTS users (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username VARCHAR(100) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    role user_role NOT NULL DEFAULT 'user',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE users IS 'Пользователи системы Diabetes Predict: обычные пользователи и администраторы.';

COMMENT ON COLUMN users.id IS 'Внутренний уникальный идентификатор пользователя.';
COMMENT ON COLUMN users.username IS 'Логин пользователя для входа в систему. Создаётся администратором.';
COMMENT ON COLUMN users.password_hash IS 'Хэш пароля пользователя. Открытый пароль в БД не хранится.';
COMMENT ON COLUMN users.role IS 'Роль пользователя: user или admin.';
COMMENT ON COLUMN users.is_active IS 'Флаг активности учётной записи. Если false, пользователь не должен иметь доступ к системе.';
COMMENT ON COLUMN users.created_at IS 'Дата и время создания пользователя.';


-- ============================================================
-- patients
-- Справочник пациентов.
-- Здесь заводятся все возможные пациенты без привязки к пользователю.
-- Связь "кто сделал предсказание для какого пациента" хранится
-- в таблице prediction_history.
-- ============================================================

CREATE TABLE IF NOT EXISTS patients (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    patient_code VARCHAR(100) NOT NULL UNIQUE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_patient_code_not_empty
        CHECK (length(trim(patient_code)) > 0)
);

COMMENT ON TABLE patients IS 'Пациенты системы Diabetes Predict';

COMMENT ON COLUMN patients.id IS 'Внутренний уникальный идентификатор пациента.';
COMMENT ON COLUMN patients.patient_code IS 'Уникальный код пациента, например PAT-001. Не должен содержать персональные данные.';
COMMENT ON COLUMN patients.is_active IS 'Флаг активности пациента. Если false, пациент скрывается из активной работы, но история прогнозов сохраняется.';
COMMENT ON COLUMN patients.created_at IS 'Дата и время создания записи пациента.';


-- ============================================================
-- datasets
-- Реестр датасетов, используемых в проекте.
-- ============================================================

CREATE TABLE IF NOT EXISTS datasets (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dataset_name VARCHAR(100) NOT NULL,
    dataset_version VARCHAR(50) NOT NULL,
    source_path VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_datasets_name_version
        UNIQUE (dataset_name, dataset_version)
);

COMMENT ON TABLE datasets IS 'Реестр наборов данных, используемых для обучения, валидации и тестирования модели.';

COMMENT ON COLUMN datasets.id IS 'Внутренний уникальный идентификатор датасета.';
COMMENT ON COLUMN datasets.dataset_name IS 'Название датасета.';
COMMENT ON COLUMN datasets.dataset_version IS 'Версия датасета для воспроизводимости экспериментов.';
COMMENT ON COLUMN datasets.source_path IS 'Путь к исходному файлу или источнику данных.';
COMMENT ON COLUMN datasets.created_at IS 'Дата и время регистрации датасета в БД.';


-- ============================================================
-- dataset_samples
-- Строки датасетов (только сырые данные).
-- Обработка (заполнение пропусков медианой) выполняется в пайплайне,
-- а не хранится в БД.
--
-- Нули в glucose, blood_pressure, skin_thickness, insulin, bmi
-- допускаются как zero-as-missing значения.
-- ============================================================
 
CREATE TABLE IF NOT EXISTS dataset_samples (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dataset_id BIGINT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    split dataset_split NOT NULL,
 
    pregnancies INTEGER NOT NULL,
    glucose NUMERIC(6, 3) NOT NULL,
    blood_pressure NUMERIC(6, 3) NOT NULL,
    skin_thickness NUMERIC(6, 3) NOT NULL,
    insulin NUMERIC(7, 3) NOT NULL,
    bmi NUMERIC(6, 3) NOT NULL,
    diabetes_pedigree_function NUMERIC(4, 3) NOT NULL,
    age INTEGER NOT NULL,
 
    outcome INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 
    CONSTRAINT chk_dataset_pregnancies_range
        CHECK (pregnancies >= 0 AND pregnancies <= 20),
 
    CONSTRAINT chk_dataset_glucose_range
        CHECK (glucose >= 0 AND glucose <= 600),
 
    CONSTRAINT chk_dataset_blood_pressure_range
        CHECK (blood_pressure >= 0 AND blood_pressure <= 200),
 
    CONSTRAINT chk_dataset_skin_thickness_range
        CHECK (skin_thickness >= 0 AND skin_thickness <= 110),
 
    CONSTRAINT chk_dataset_insulin_range
        CHECK (insulin >= 0 AND insulin <= 1000),
 
    CONSTRAINT chk_dataset_bmi_range
        CHECK (bmi >= 0 AND bmi <= 100),
 
    CONSTRAINT chk_dataset_diabetes_pedigree_range
        CHECK (diabetes_pedigree_function >= 0 AND diabetes_pedigree_function <= 3),
 
    CONSTRAINT chk_dataset_age_range
        CHECK (age > 0 AND age <= 120),
 
    CONSTRAINT chk_dataset_outcome_value
        CHECK (outcome IN (0, 1))
);
 
CREATE INDEX IF NOT EXISTS idx_dataset_samples_ds_split
    ON dataset_samples(dataset_id, split);
 
COMMENT ON TABLE dataset_samples IS 'Строки датасетов с медицинскими признаками и целевой меткой.';
 
COMMENT ON COLUMN dataset_samples.id IS 'Внутренний уникальный идентификатор строки датасета.';
COMMENT ON COLUMN dataset_samples.dataset_id IS 'Идентификатор датасета, которому принадлежит строка.';
COMMENT ON COLUMN dataset_samples.split IS 'Назначение строки датасета: train, valid или test.';
COMMENT ON COLUMN dataset_samples.pregnancies IS 'Количество беременностей.';
COMMENT ON COLUMN dataset_samples.glucose IS 'Уровень глюкозы. Значение 0 допускается как пропуск / не измерено.';
COMMENT ON COLUMN dataset_samples.blood_pressure IS 'Артериальное давление. Значение 0 допускается как пропуск / не измерено.';
COMMENT ON COLUMN dataset_samples.skin_thickness IS 'Толщина кожной складки. Значение 0 допускается как пропуск / не измерено.';
COMMENT ON COLUMN dataset_samples.insulin IS 'Уровень инсулина. Значение 0 допускается как пропуск / не измерено.';
COMMENT ON COLUMN dataset_samples.bmi IS 'Индекс массы тела. Значение 0 допускается как пропуск / не измерено.';
COMMENT ON COLUMN dataset_samples.diabetes_pedigree_function IS 'Наследственный фактор диабета.';
COMMENT ON COLUMN dataset_samples.age IS 'Возраст пациента.';
COMMENT ON COLUMN dataset_samples.outcome IS 'Истинная метка из датасета: 0 — диабет не выявлен, 1 — диабет выявлен.';
COMMENT ON COLUMN dataset_samples.created_at IS 'Дата и время добавления строки датасета в БД.';


-- ============================================================
-- model_versions
-- Реестр версий моделей.
-- Роль определяет назначение версии: champion обслуживает прод,
-- challenger тестируется, archived выведена из эксплуатации.
-- ============================================================

CREATE TABLE IF NOT EXISTS model_versions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    model_name VARCHAR(100) NOT NULL,
    model_version VARCHAR(50) NOT NULL UNIQUE,
    artifact_path VARCHAR(255) NOT NULL,
    artifact_sha256 VARCHAR(64) NOT NULL,
    train_medians JSONB NOT NULL,
    preprocessing_version VARCHAR(50),
    trained_on_dataset_id BIGINT REFERENCES datasets(id) ON DELETE SET NULL,

    accuracy_score NUMERIC(18, 16),
    precision_score NUMERIC(18, 16),
    recall_score NUMERIC(18, 16),
    f1_score NUMERIC(18, 16),
    params_json JSONB,
 
    traffic_weight INTEGER NOT NULL DEFAULT 100,
    role model_role NOT NULL DEFAULT 'challenger',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_model_traffic_weight
        CHECK (traffic_weight >= 0 AND traffic_weight <= 100),
 
    CONSTRAINT chk_model_accuracy_range
        CHECK (accuracy_score IS NULL OR (accuracy_score >= 0 AND accuracy_score <= 1)),
 
    CONSTRAINT chk_model_precision_range
        CHECK (precision_score IS NULL OR (precision_score >= 0 AND precision_score <= 1)),
 
    CONSTRAINT chk_model_recall_range
        CHECK (recall_score IS NULL OR (recall_score >= 0 AND recall_score <= 1)),
 
    CONSTRAINT chk_model_f1_range
        CHECK (f1_score IS NULL OR (f1_score >= 0 AND f1_score <= 1))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_model_versions_single_champion
    ON model_versions (role)
    WHERE role = 'champion';

COMMENT ON TABLE model_versions IS 'Версии ML-моделей';

COMMENT ON COLUMN model_versions.id IS 'Внутренний уникальный идентификатор версии модели.';
COMMENT ON COLUMN model_versions.model_name IS 'Название модели, например LogisticRegressionTuned.';
COMMENT ON COLUMN model_versions.model_version IS 'Версия модели, например lab2-1.0.0.';
COMMENT ON COLUMN model_versions.artifact_path IS 'Путь к файлу модели joblib внутри проекта или контейнера.';
COMMENT ON COLUMN model_versions.preprocessing_version IS 'Версия пайплайна предобработки данных.';
COMMENT ON COLUMN model_versions.trained_on_dataset_id IS 'Датасет, на котором была обучена модель.';
COMMENT ON COLUMN model_versions.accuracy_score IS 'Accuracy модели.';
COMMENT ON COLUMN model_versions.precision_score IS 'Precision модели.';
COMMENT ON COLUMN model_versions.recall_score IS 'Recall модели.';
COMMENT ON COLUMN model_versions.f1_score IS 'F1-score модели.';
COMMENT ON COLUMN model_versions.params_json IS 'Гиперпараметры и дополнительные параметры модели в формате JSON.';
COMMENT ON COLUMN model_versions.traffic_weight IS 'Вес трафика для будущего A/B testing моделей.';
COMMENT ON COLUMN model_versions.role IS 'Роль версии модели: champion, challenger или archived.';
COMMENT ON COLUMN model_versions.created_at IS 'Дата и время регистрации версии модели.';


-- ============================================================
-- prediction_history
-- История всех предсказаний. Главная таблица инференса.
--
-- patient_code_snapshot и model_version_snapshot сохраняют контекст
-- на момент предсказания, чтобы история не терялась после удаления
-- пациента или версии модели.
-- ============================================================
 
CREATE TABLE IF NOT EXISTS studies (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    patient_code VARCHAR(6) NOT NULL CHECK (patient_code ~ '^[A-Z]{3}[0-9]{3}$'),
    study_date DATE NOT NULL,
    features JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_studies_patient_date UNIQUE (patient_code, study_date)
);

CREATE TABLE IF NOT EXISTS prediction_history (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    study_id BIGINT NOT NULL REFERENCES studies(id) ON DELETE RESTRICT,
 
    user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
    patient_id BIGINT REFERENCES patients(id) ON DELETE SET NULL,
    model_version_id BIGINT REFERENCES model_versions(id) ON DELETE SET NULL,
 
    patient_code_snapshot VARCHAR(100),
    model_version_snapshot VARCHAR(50) NOT NULL,
    study_date DATE NOT NULL,
    inference_payload JSONB,
 
    pregnancies INTEGER NOT NULL,
    glucose NUMERIC(6, 3) NOT NULL,
    blood_pressure NUMERIC(6, 3) NOT NULL,
    skin_thickness NUMERIC(6, 3) NOT NULL,
    insulin NUMERIC(7, 3) NOT NULL,
    bmi NUMERIC(6, 3) NOT NULL,
    diabetes_pedigree_function NUMERIC(4, 3) NOT NULL,
    age INTEGER NOT NULL,
 
    prediction INTEGER NOT NULL,
    probability NUMERIC(6, 5),
    label prediction_label NOT NULL,
 
    request_source request_source NOT NULL DEFAULT 'frontend',
    response_time_ms INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 
    CONSTRAINT chk_prediction_pregnancies_range
        CHECK (pregnancies >= 0 AND pregnancies <= 20),
 
    CONSTRAINT chk_prediction_glucose_range
        CHECK (glucose >= 0 AND glucose <= 600),
 
    CONSTRAINT chk_prediction_blood_pressure_range
        CHECK (blood_pressure >= 0 AND blood_pressure <= 200),
 
    CONSTRAINT chk_prediction_skin_thickness_range
        CHECK (skin_thickness >= 0 AND skin_thickness <= 110),
 
    CONSTRAINT chk_prediction_insulin_range
        CHECK (insulin >= 0 AND insulin <= 1000),
 
    CONSTRAINT chk_prediction_bmi_range
        CHECK (bmi >= 0 AND bmi <= 100),
 
    CONSTRAINT chk_prediction_diabetes_pedigree_range
        CHECK (diabetes_pedigree_function >= 0 AND diabetes_pedigree_function <= 3),
 
    CONSTRAINT chk_prediction_age_range
        CHECK (age > 0 AND age <= 120),
 
    CONSTRAINT chk_prediction_value
        CHECK (prediction IN (0, 1)),
 
    CONSTRAINT chk_prediction_probability_range
        CHECK (probability IS NULL OR (probability >= 0 AND probability <= 1)),
 
    CONSTRAINT chk_prediction_response_time
        CHECK (response_time_ms IS NULL OR response_time_ms >= 0)
);
 
CREATE INDEX IF NOT EXISTS idx_pred_hist_user
    ON prediction_history(user_id);

-- Один прогноз версии модели для исследования пациента на указанную дату.
CREATE UNIQUE INDEX IF NOT EXISTS uq_prediction_history_study_model
    ON prediction_history (study_id, model_version_snapshot);
 
CREATE INDEX IF NOT EXISTS idx_pred_hist_patient
    ON prediction_history(patient_id);
 
CREATE INDEX IF NOT EXISTS idx_pred_hist_created_at
    ON prediction_history(created_at);
 
CREATE INDEX IF NOT EXISTS idx_pred_hist_model_created_at
    ON prediction_history(model_version_id, created_at);
 
COMMENT ON TABLE prediction_history IS 'История инференса: входные признаки, результат модели, пользователь, пациент и версия модели.';
 
COMMENT ON COLUMN prediction_history.id IS 'Внутренний уникальный идентификатор предсказания.';
COMMENT ON COLUMN prediction_history.user_id IS 'Пользователь, выполнивший предсказание. Может быть NULL, если пользователь удалён.';
COMMENT ON COLUMN prediction_history.patient_id IS 'Пациент, для которого выполнено предсказание. Может быть NULL, если пациент удалён.';
COMMENT ON COLUMN prediction_history.model_version_id IS 'Версия модели, которая выполнила предсказание. Может быть NULL, если запись версии модели удалена.';
COMMENT ON COLUMN prediction_history.patient_code_snapshot IS 'Код пациента на момент выполнения предсказания. Нужен для сохранения истории даже после удаления пациента.';
COMMENT ON COLUMN prediction_history.model_version_snapshot IS 'Версия модели на момент выполнения предсказания. Нужна для сохранения истории даже после удаления записи модели.';
COMMENT ON COLUMN prediction_history.pregnancies IS 'Количество беременностей, переданное в модель.';
COMMENT ON COLUMN prediction_history.glucose IS 'Уровень глюкозы, переданный в модель. Значение 0 допускается как пропуск / не измерено.';
COMMENT ON COLUMN prediction_history.blood_pressure IS 'Артериальное давление, переданное в модель. Значение 0 допускается как пропуск / не измерено.';
COMMENT ON COLUMN prediction_history.skin_thickness IS 'Толщина кожной складки, переданная в модель. Значение 0 допускается как пропуск / не измерено.';
COMMENT ON COLUMN prediction_history.insulin IS 'Уровень инсулина, переданный в модель. Значение 0 допускается как пропуск / не измерено.';
COMMENT ON COLUMN prediction_history.bmi IS 'Индекс массы тела, переданный в модель. Значение 0 допускается как пропуск / не измерено.';
COMMENT ON COLUMN prediction_history.diabetes_pedigree_function IS 'Наследственный фактор диабета, переданный в модель.';
COMMENT ON COLUMN prediction_history.age IS 'Возраст пациента, переданный в модель.';
COMMENT ON COLUMN prediction_history.prediction IS 'Числовой результат модели: 0 или 1.';
COMMENT ON COLUMN prediction_history.probability IS 'Вероятность положительного класса, возвращённая моделью.';
COMMENT ON COLUMN prediction_history.label IS 'Текстовая метка результата: detected или not_detected.';
COMMENT ON COLUMN prediction_history.request_source IS 'Источник запроса: frontend, api или test.';
COMMENT ON COLUMN prediction_history.response_time_ms IS 'Время обработки запроса в миллисекундах.';
COMMENT ON COLUMN prediction_history.created_at IS 'Дата и время выполнения предсказания.';


-- ============================================================
-- prediction_feedback
-- Обратная связь по исследованию (общая истинная метка для всех моделей).
-- Связь 1:1 со studies. При поступлении более надёжного
-- источника запись перезаписывается (UPDATE).
-- ============================================================
 
CREATE TABLE IF NOT EXISTS prediction_feedback (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    study_id BIGINT NOT NULL UNIQUE
        REFERENCES studies(id) ON DELETE RESTRICT,
    created_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
 
    true_label INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 
    CONSTRAINT chk_feedback_true_label_value
        CHECK (true_label IN (0, 1))
);
 
COMMENT ON TABLE prediction_feedback IS 'Истинные метки по ранее выполненным предсказаниям. Используется для оценки качества модели на реальных данных.';
 
COMMENT ON COLUMN prediction_feedback.id IS 'Внутренний уникальный идентификатор обратной связи.';
COMMENT ON COLUMN prediction_feedback.study_id IS 'Исследование, к которому относится общая фактическая метка для всех моделей.';
COMMENT ON COLUMN prediction_feedback.created_by_user_id IS 'Пользователь, внёсший истинную метку. Может быть NULL, если автор неизвестен или пользователь удалён. Роль автора берётся из users.role.';
COMMENT ON COLUMN prediction_feedback.true_label IS 'Реальная метка: 0 — диабет не подтверждён, 1 — диабет подтверждён.';
COMMENT ON COLUMN prediction_feedback.created_at IS 'Дата и время добавления/обновления обратной связи.';
