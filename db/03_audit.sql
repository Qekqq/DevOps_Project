-- История изменений фиксируется в той же транзакции, что и изменение.
CREATE FUNCTION audit_feedback() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO feedback_history(study_id, old_label, new_label, changed_by)
        VALUES (NEW.study_id, NULL, NEW.true_label, NEW.created_by_user_id);
    ELSIF OLD.true_label IS DISTINCT FROM NEW.true_label THEN
        NEW.updated_at = now();
        INSERT INTO feedback_history(study_id, old_label, new_label, changed_by)
        VALUES (NEW.study_id, OLD.true_label, NEW.true_label, NEW.created_by_user_id);
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER feedback_audit BEFORE INSERT OR UPDATE ON feedback
FOR EACH ROW EXECUTE FUNCTION audit_feedback();

CREATE FUNCTION audit_model_role() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE actor bigint;
BEGIN
    actor := NULLIF(current_setting('app.actor_id', true), '')::bigint;
    IF TG_OP = 'INSERT' THEN
        INSERT INTO model_role_history(model_version_id, old_role, new_role, changed_by)
        VALUES (NEW.id, NULL, NEW.role, actor);
    ELSIF OLD.role IS DISTINCT FROM NEW.role THEN
        INSERT INTO model_role_history(model_version_id, old_role, new_role, changed_by)
        VALUES (NEW.id, OLD.role, NEW.role, actor);
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER model_role_audit AFTER INSERT OR UPDATE ON model_versions
FOR EACH ROW EXECUTE FUNCTION audit_model_role();

CREATE FUNCTION forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Records in % are immutable; create a new version', TG_TABLE_NAME;
END $$;
CREATE TRIGGER immutable_dataset BEFORE UPDATE OR DELETE ON datasets
FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER immutable_dataset_rows BEFORE UPDATE OR DELETE ON dataset_rows
FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER immutable_training BEFORE UPDATE OR DELETE ON training_runs
FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER immutable_predictions BEFORE UPDATE OR DELETE ON predictions
FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER immutable_studies BEFORE UPDATE OR DELETE ON studies
FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER immutable_feedback_history BEFORE UPDATE OR DELETE ON feedback_history
FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER immutable_role_history BEFORE UPDATE OR DELETE ON model_role_history
FOR EACH ROW EXECUTE FUNCTION forbid_change();

CREATE FUNCTION protect_model_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Archive model versions instead of deleting';
    END IF;
    IF (to_jsonb(NEW) - 'role') IS DISTINCT FROM (to_jsonb(OLD) - 'role') THEN
        RAISE EXCEPTION 'Only the role of a registered model version can change';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER immutable_model BEFORE UPDATE OR DELETE ON model_versions
FOR EACH ROW EXECUTE FUNCTION protect_model_version();

CREATE FUNCTION protect_feedback_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Correct feedback instead of deleting it';
    END IF;
    IF (to_jsonb(NEW) - ARRAY['true_label', 'created_by_user_id', 'updated_at'])
       IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['true_label', 'created_by_user_id', 'updated_at']) THEN
        RAISE EXCEPTION 'Feedback must remain attached to its original study';
    END IF;
    IF NEW.true_label = OLD.true_label THEN
        NEW.updated_at := OLD.updated_at;
        NEW.created_by_user_id := OLD.created_by_user_id;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER feedback_identity_guard BEFORE UPDATE OR DELETE ON feedback
FOR EACH ROW EXECUTE FUNCTION protect_feedback_identity();
