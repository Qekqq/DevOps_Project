BEGIN;
ALTER TABLE datasets ADD COLUMN IF NOT EXISTS source_sha256 VARCHAR(64);
ALTER TABLE datasets ADD COLUMN IF NOT EXISTS row_count INTEGER;
CREATE UNIQUE INDEX IF NOT EXISTS uq_datasets_source_sha256 ON datasets(source_sha256);
CREATE TABLE IF NOT EXISTS raw_dataset_samples (
    dataset_id BIGINT NOT NULL REFERENCES datasets(id) ON DELETE RESTRICT,
    row_number INTEGER NOT NULL CHECK (row_number >= 0),
    sample_values JSONB NOT NULL,
    PRIMARY KEY (dataset_id, row_number)
);
COMMIT;
