-- Применять вручную при остановленных API и consumer.
-- Существующие записи сохраняются. Их даты нужно заполнить отдельно
-- по известным датам исследований, а не по created_at.
BEGIN;
ALTER TABLE prediction_history ADD COLUMN IF NOT EXISTS study_date DATE;
ALTER TABLE prediction_history ADD COLUMN IF NOT EXISTS inference_payload JSONB;

CREATE UNIQUE INDEX IF NOT EXISTS uq_prediction_history_study_model
    ON prediction_history (upper(btrim(patient_code_snapshot)), study_date, model_version_snapshot);
DROP INDEX IF EXISTS uq_prediction_history_patient_code;
COMMIT;

-- После заполнения и проверки ВСЕХ старых дат, до запуска приложения:
-- ALTER TABLE prediction_history ALTER COLUMN study_date SET NOT NULL;
-- inference_payload у старых строк остаётся NULL: первоначальную точность
-- ранее округлённых значений восстановить невозможно.
