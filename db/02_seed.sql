-- ============================================================
-- 02_seed.sql
-- Initial reference data for DevOps HW 2 / Diabetes Predict
--
-- Назначение:
--   - добавить начальную версию модели из лабораторной работы №1;
--   - сделать её champion-моделью;
--   - сохранить метрики валидации;
--   - сохранить гиперпараметры модели в params_json.
-- ============================================================


-- Пример кода пациента. Прогноз для него заранее не создаётся.
INSERT INTO patients (patient_code)
VALUES ('PAT001')
ON CONFLICT (patient_code) DO NOTHING;

INSERT INTO model_versions (
    model_name,
    model_version,
    artifact_path,
    artifact_sha256,
    train_medians,
    preprocessing_version,
    trained_on_dataset_id,
    accuracy_score,
    precision_score,
    recall_score,
    f1_score,
    params_json,
    traffic_weight,
    role
)
VALUES (
    'LogisticRegressionTuned',
    'lab2-1.0.0',
    'experiments/best_model/model.joblib',
    'efac22a1144e60e62156f42b7da6407d0ba4f091aec9b6633c5085bd84c75e41',
    '{"glucose": 117.0, "blood_pressure": 72.0, "skin_thickness": 30.0, "insulin": 126.0, "bmi": 32.4}'::jsonb,
    'preprocessing-v1',
    NULL,
    0.7931034482758621,
    0.6734693877551020,
    0.8048780487804879,
    0.7333333333333333,
    '{
        "C": 1,
        "penalty": "l1",
        "class_weight": "balanced",
        "solver": "liblinear",
        "max_iter": 1000
    }'::jsonb,
    100,
    'champion'
)
ON CONFLICT (model_version) DO NOTHING;

INSERT INTO model_versions (model_name, model_version, artifact_path, artifact_sha256,
    train_medians, preprocessing_version, accuracy_score, precision_score, recall_score,
    f1_score, params_json, traffic_weight, role)
VALUES ('DecisionTreeClassifier', 'decision-tree-v1', 'experiments/decision_tree/model.joblib', 'eb2402d32f3d5c6a4dc0fcc6939b31a43402cf819a32d2936a070273ab78eab4',
    '{"glucose": 117.0, "blood_pressure": 72.0, "skin_thickness": 30.0, "insulin": 126.0, "bmi": 32.4}'::jsonb, 'preprocessing-v1',
    0.7758620689655172, 0.6470588235294118, 0.8048780487804879, 0.717391304347826,
    '{}'::jsonb, 0, 'challenger')
ON CONFLICT (model_version) DO NOTHING;

INSERT INTO model_versions (model_name, model_version, artifact_path, artifact_sha256,
    train_medians, preprocessing_version, accuracy_score, precision_score, recall_score,
    f1_score, params_json, traffic_weight, role)
VALUES ('DecisionTreeClassifierTuned', 'decision-tree-tuned-v1', 'experiments/decision_tree_tuned/model.joblib', '20c22151f26e0cebb869e5c773463437050c21f62e6dc4e6887a3bacfc7a0e60',
    '{"glucose": 117.0, "blood_pressure": 72.0, "skin_thickness": 30.0, "insulin": 126.0, "bmi": 32.4}'::jsonb, 'preprocessing-v1',
    0.7672413793103449, 0.6206896551724138, 0.8780487804878049, 0.7272727272727273,
    '{"class_weight": "balanced", "criterion": "gini", "max_depth": 4, "min_samples_leaf": 10, "min_samples_split": 2}'::jsonb, 0, 'challenger')
ON CONFLICT (model_version) DO NOTHING;

INSERT INTO model_versions (model_name, model_version, artifact_path, artifact_sha256,
    train_medians, preprocessing_version, accuracy_score, precision_score, recall_score,
    f1_score, params_json, traffic_weight, role)
VALUES ('LogisticRegression', 'logistic-regression-v1', 'experiments/logistic_regression/model.joblib', '9d03a5b96e162ae9bf6ac1f5e7a9e1d38458f2452881fa13da940d3a8a72ad2f',
    '{"glucose": 117.0, "blood_pressure": 72.0, "skin_thickness": 30.0, "insulin": 126.0, "bmi": 32.4}'::jsonb, 'preprocessing-v1',
    0.8189655172413793, 0.7777777777777778, 0.6829268292682927, 0.7272727272727273,
    '{}'::jsonb, 0, 'challenger')
ON CONFLICT (model_version) DO NOTHING;

-- Backfill only the known original artifact; preserve its role and version.
UPDATE model_versions SET artifact_sha256 = 'efac22a1144e60e62156f42b7da6407d0ba4f091aec9b6633c5085bd84c75e41',
    train_medians = '{"glucose": 117.0, "blood_pressure": 72.0, "skin_thickness": 30.0, "insulin": 126.0, "bmi": 32.4}'::jsonb
WHERE model_version = 'lab2-1.0.0'
    AND artifact_path = 'experiments/best_model/model.joblib'
    AND artifact_sha256 IS NULL AND train_medians IS NULL;
