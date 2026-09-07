-- Историческая миграция ограничения «один пациент — один прогноз».
-- Для новой логики после неё применяется 002_prediction_study.sql.
BEGIN;
CREATE UNIQUE INDEX IF NOT EXISTS uq_prediction_history_patient_code
    ON prediction_history (upper(btrim(patient_code_snapshot)));
COMMIT;
