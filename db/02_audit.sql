-- История изменений фиксируется в той же транзакции, что и изменение.
CREATE FUNCTION audit_feedback() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO public.feedback_history(study_id, old_label, new_label, changed_by)
        VALUES (NEW.study_id, NULL, NEW.true_label, NEW.created_by_user_id);
    ELSIF OLD.true_label IS DISTINCT FROM NEW.true_label THEN
        NEW.updated_at = now();
        INSERT INTO public.feedback_history(study_id, old_label, new_label, changed_by)
        VALUES (NEW.study_id, OLD.true_label, NEW.true_label, NEW.created_by_user_id);
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER feedback_audit BEFORE INSERT OR UPDATE ON feedback
FOR EACH ROW EXECUTE FUNCTION audit_feedback();

CREATE FUNCTION audit_model_role() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE actor bigint;
BEGIN
    actor := NULLIF(current_setting('app.actor_id', true), '')::bigint;
    IF TG_OP = 'INSERT' THEN
        INSERT INTO public.model_role_history(model_version_id, old_role, new_role, changed_by)
        VALUES (NEW.id, NULL, NEW.role, actor);
    ELSIF OLD.role IS DISTINCT FROM NEW.role THEN
        INSERT INTO public.model_role_history(model_version_id, old_role, new_role, changed_by)
        VALUES (NEW.id, OLD.role, NEW.role, actor);
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER model_role_audit AFTER INSERT OR UPDATE ON model_versions
FOR EACH ROW EXECUTE FUNCTION audit_model_role();

CREATE FUNCTION forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Записи в % неизменяемы; создайте новую версию', TG_TABLE_NAME;
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
        RAISE EXCEPTION 'Версии моделей нужно архивировать, а не удалять';
    END IF;
    IF (to_jsonb(NEW) - 'role') IS DISTINCT FROM (to_jsonb(OLD) - 'role') THEN
        RAISE EXCEPTION 'У зарегистрированной версии модели можно изменить только роль';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER immutable_model BEFORE UPDATE OR DELETE ON model_versions
FOR EACH ROW EXECUTE FUNCTION protect_model_version();

CREATE FUNCTION protect_feedback_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Обратную связь можно исправить, но нельзя удалить';
    END IF;
    IF (to_jsonb(NEW) - ARRAY['true_label', 'created_by_user_id', 'updated_at'])
       IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['true_label', 'created_by_user_id', 'updated_at']) THEN
        RAISE EXCEPTION 'Обратная связь должна оставаться связанной с исходным исследованием';
    END IF;
    IF NEW.true_label = OLD.true_label THEN
        NEW.updated_at := OLD.updated_at;
        NEW.created_by_user_id := OLD.created_by_user_id;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER feedback_identity_guard BEFORE UPDATE OR DELETE ON feedback
FOR EACH ROW EXECUTE FUNCTION protect_feedback_identity();

-- Правка показателей разрешена только в транзакции административного пересчёта.
CREATE OR REPLACE FUNCTION protect_study_recalculation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' OR current_setting('app.recalculate_study', true) IS DISTINCT FROM 'on' THEN
        RAISE EXCEPTION 'Для изменения данных исследования используйте административный пересчёт';
    END IF;
    IF TG_TABLE_NAME = 'studies' AND
       (to_jsonb(NEW) - ARRAY['pregnancies','glucose','blood_pressure','skin_thickness','insulin','bmi','diabetes_pedigree_function','age'])
       IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY['pregnancies','glucose','blood_pressure','skin_thickness','insulin','bmi','diabetes_pedigree_function','age']) THEN
        RAISE EXCEPTION 'Код пациента и дату исследования менять нельзя';
    END IF;
    IF TG_TABLE_NAME = 'predictions' AND
       (to_jsonb(NEW) - ARRAY['prediction','probability','response_time_ms'])
       IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['prediction','probability','response_time_ms']) THEN
        RAISE EXCEPTION 'Исследование, версию и исходную роль прогноза менять нельзя';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS immutable_studies ON studies;
CREATE TRIGGER immutable_studies BEFORE UPDATE OR DELETE ON studies
FOR EACH ROW EXECUTE FUNCTION protect_study_recalculation();
DROP TRIGGER IF EXISTS immutable_predictions ON predictions;
CREATE TRIGGER immutable_predictions BEFORE UPDATE OR DELETE ON predictions
FOR EACH ROW EXECUTE FUNCTION protect_study_recalculation();
DROP TRIGGER IF EXISTS immutable_study_edits ON study_edits;
CREATE TRIGGER immutable_study_edits BEFORE UPDATE OR DELETE ON study_edits
FOR EACH ROW EXECUTE FUNCTION forbid_change();
