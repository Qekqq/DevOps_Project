-- Только добавляет поля. Затем применить актуальный 02_seed.sql,
-- проверить заполнение всех версий и установить NOT NULL.
BEGIN;
ALTER TABLE model_versions ADD COLUMN IF NOT EXISTS artifact_sha256 VARCHAR(64);
ALTER TABLE model_versions ADD COLUMN IF NOT EXISTS train_medians JSONB;
COMMIT;
-- После регистрации и проверки всех имеющихся версий:
-- ALTER TABLE model_versions ALTER COLUMN artifact_sha256 SET NOT NULL;
-- ALTER TABLE model_versions ALTER COLUMN train_medians SET NOT NULL;
