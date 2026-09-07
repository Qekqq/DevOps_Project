-- После 002 и явного заполнения дат. API и consumer должны быть остановлены.
-- Выполнять с psql ON_ERROR_STOP=1. При противоречиях транзакция откатывается.
BEGIN;
LOCK TABLE prediction_history, prediction_feedback IN ACCESS EXCLUSIVE MODE;
ALTER TABLE prediction_history ALTER COLUMN study_date SET NOT NULL;

CREATE TEMP TABLE migration_studies ON COMMIT DROP AS
SELECT id AS prediction_id, upper(btrim(patient_code_snapshot)) AS patient_code,
       study_date,
       COALESCE(inference_payload->'features', jsonb_build_object(
           'pregnancies', pregnancies, 'glucose', glucose,
           'blood_pressure', blood_pressure, 'skin_thickness', skin_thickness,
           'insulin', insulin, 'bmi', bmi,
           'diabetes_pedigree_function', diabetes_pedigree_function, 'age', age
       )) AS features
FROM prediction_history;

DO $$ BEGIN
    IF EXISTS (
        SELECT 1 FROM migration_studies GROUP BY patient_code, study_date
        HAVING count(DISTINCT features) > 1
    ) THEN
        RAISE EXCEPTION 'Different features for the same study; resolve manually';
    END IF;
    IF EXISTS (
        SELECT 1 FROM prediction_feedback f
        JOIN migration_studies s ON s.prediction_id = f.prediction_history_id
        GROUP BY s.patient_code, s.study_date HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION 'Multiple feedback records for the same study; resolve manually';
    END IF;
END $$;

CREATE TABLE studies (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    patient_code VARCHAR(6) NOT NULL CHECK (patient_code ~ '^[A-Z]{3}[0-9]{3}$'),
    study_date DATE NOT NULL,
    features JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_studies_patient_date UNIQUE (patient_code, study_date)
);
INSERT INTO studies(patient_code, study_date, features)
SELECT DISTINCT patient_code, study_date, features FROM migration_studies;

ALTER TABLE prediction_history ADD COLUMN study_id BIGINT REFERENCES studies(id) ON DELETE RESTRICT;
UPDATE prediction_history p SET study_id = s.id FROM studies s
WHERE upper(btrim(p.patient_code_snapshot)) = s.patient_code AND p.study_date = s.study_date;
ALTER TABLE prediction_history ALTER COLUMN study_id SET NOT NULL;
ALTER TABLE prediction_history ALTER COLUMN model_version_snapshot SET NOT NULL;
DROP INDEX IF EXISTS uq_prediction_history_study_model;
CREATE UNIQUE INDEX uq_prediction_history_study_model
    ON prediction_history(study_id, model_version_snapshot);

ALTER TABLE prediction_feedback ADD COLUMN study_id BIGINT REFERENCES studies(id) ON DELETE RESTRICT;
UPDATE prediction_feedback f SET study_id = p.study_id
FROM prediction_history p WHERE p.id = f.prediction_history_id;
ALTER TABLE prediction_feedback ALTER COLUMN study_id SET NOT NULL;
ALTER TABLE prediction_feedback ADD CONSTRAINT uq_feedback_study UNIQUE(study_id);
ALTER TABLE prediction_feedback DROP COLUMN prediction_history_id;
COMMIT;
