-- Поддержка полных pipeline: медианы хранятся внутри артефакта.
BEGIN;
ALTER TABLE model_versions ALTER COLUMN train_medians DROP NOT NULL;
ALTER TABLE model_versions ADD COLUMN IF NOT EXISTS artifact_format VARCHAR(30) NOT NULL DEFAULT 'legacy-v1';
ALTER TABLE model_versions ADD COLUMN IF NOT EXISTS metadata_json JSONB;
COMMIT;
