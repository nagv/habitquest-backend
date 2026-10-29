-- Editorial workflow (plan 3): editor role, two-person lifecycle with audit, release build/publish,
-- emergency withdrawal. Editors write drafts directly; every status change goes through a function.

DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='hq_editor') THEN CREATE ROLE hq_editor NOLOGIN; END IF;
END $$;
GRANT USAGE ON SCHEMA hq TO hq_editor;

CREATE OR REPLACE FUNCTION hq.my_editor() RETURNS uuid LANGUAGE sql STABLE AS $$
  SELECT NULLIF(current_setting('app.editor_id',true),'')::uuid $$;

CREATE TABLE hq.catalog_audit(
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity text NOT NULL CHECK(entity IN ('goal','concern','habit','chapter')),
  entity_id text NOT NULL, version int NOT NULL,
  from_status text NOT NULL, to_status text NOT NULL,
  actor uuid NOT NULL, reason text, at timestamptz NOT NULL DEFAULT now());

-- Versions under review are frozen; only the lifecycle functions move them on.
CREATE FUNCTION hq.guard_review_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.status = 'in_review' AND NEW.status = 'in_review' THEN
    RAISE EXCEPTION 'Versions in review cannot be edited' USING ERRCODE = '55000';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER frozen_in_review BEFORE UPDATE ON hq.goals
  FOR EACH ROW EXECUTE FUNCTION hq.guard_review_version();
CREATE TRIGGER frozen_in_review BEFORE UPDATE ON hq.concerns
  FOR EACH ROW EXECUTE FUNCTION hq.guard_review_version();
CREATE TRIGGER frozen_in_review BEFORE UPDATE ON hq.habit_versions
  FOR EACH ROW EXECUTE FUNCTION hq.guard_review_version();
CREATE TRIGGER frozen_in_review BEFORE UPDATE ON hq.chapter_templates
  FOR EACH ROW EXECUTE FUNCTION hq.guard_review_version();

CREATE OR REPLACE FUNCTION hq.guard_habit_detail() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE parent_status text;
BEGIN
  IF TG_OP <> 'INSERT' THEN
    SELECT v.status INTO parent_status FROM hq.habit_versions v
      WHERE v.habit_id = OLD.habit_id AND v.version = OLD.version FOR SHARE;
    IF parent_status = 'approved' THEN
      RAISE EXCEPTION 'Approved catalog versions are immutable' USING ERRCODE = '42501';
    ELSIF parent_status = 'in_review' THEN
      RAISE EXCEPTION 'Versions in review cannot be edited' USING ERRCODE = '55000';
    END IF;
  END IF;
  IF TG_OP <> 'DELETE' THEN
    SELECT v.status INTO parent_status FROM hq.habit_versions v
      WHERE v.habit_id = NEW.habit_id AND v.version = NEW.version FOR SHARE;
    IF parent_status = 'approved' THEN
      RAISE EXCEPTION 'Approved catalog versions are immutable' USING ERRCODE = '42501';
    ELSIF parent_status = 'in_review' THEN
      RAISE EXCEPTION 'Versions in review cannot be edited' USING ERRCODE = '55000';
    END IF;
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION hq.guard_concern_detail() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE parent_status text;
BEGIN
  IF TG_OP <> 'INSERT' THEN
    SELECT c.status INTO parent_status FROM hq.concerns c
      WHERE c.id = OLD.concern_id AND c.version = OLD.concern_version FOR SHARE;
    IF parent_status = 'approved' THEN
      RAISE EXCEPTION 'Approved catalog versions are immutable' USING ERRCODE = '42501';
    ELSIF parent_status = 'in_review' THEN
      RAISE EXCEPTION 'Versions in review cannot be edited' USING ERRCODE = '55000';
    END IF;
  END IF;
  IF TG_OP <> 'DELETE' THEN
    SELECT c.status INTO parent_status FROM hq.concerns c
      WHERE c.id = NEW.concern_id AND c.version = NEW.concern_version FOR SHARE;
    IF parent_status = 'approved' THEN
      RAISE EXCEPTION 'Approved catalog versions are immutable' USING ERRCODE = '42501';
    ELSIF parent_status = 'in_review' THEN
      RAISE EXCEPTION 'Versions in review cannot be edited' USING ERRCODE = '55000';
    END IF;
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;

-- Editors cannot set status, reviewer or withdrawal columns, and the author is always the caller.
ALTER TABLE hq.goals ALTER COLUMN created_by SET DEFAULT hq.my_editor();
ALTER TABLE hq.concerns ALTER COLUMN created_by SET DEFAULT hq.my_editor();
ALTER TABLE hq.habit_versions ALTER COLUMN created_by SET DEFAULT hq.my_editor();
ALTER TABLE hq.chapter_templates ALTER COLUMN created_by SET DEFAULT hq.my_editor();
GRANT SELECT, INSERT ON hq.habits TO hq_editor;
GRANT SELECT ON hq.goals, hq.concerns, hq.habit_versions, hq.chapter_templates, hq.catalog_releases,
  hq.release_items, hq.catalog_audit, hq.catalog_requests, hq.current_release TO hq_editor;
GRANT INSERT(id, version, title, description), UPDATE(title, description) ON hq.goals TO hq_editor;
GRANT INSERT(id, version, label, synonyms), UPDATE(label, synonyms) ON hq.concerns TO hq_editor;
GRANT INSERT(habit_id, version, title, instruction, min_age, max_age, routine_anchor, safety_notes),
  UPDATE(title, instruction, min_age, max_age, routine_anchor, safety_notes) ON hq.habit_versions TO hq_editor;
GRANT INSERT(id, version, world, goal_id, title), UPDATE(world, goal_id, title)
  ON hq.chapter_templates TO hq_editor;
GRANT SELECT, INSERT, DELETE ON hq.concern_goals, hq.habit_levels, hq.habit_goals, hq.habit_prereqs,
  hq.habit_tags, hq.habit_variants TO hq_editor;

-- Version details for any status (editor only), and release contents for any release, published or not.
CREATE VIEW hq.habit_version_details AS
SELECT v.habit_id, v.version, v.title, v.instruction, v.min_age, v.max_age, v.routine_anchor, v.safety_notes,
  v.status, v.created_by, v.reviewed_by, v.reviewed_at, v.withdrawn_at, v.withdrawn_reason,
  (SELECT coalesce(jsonb_agg(jsonb_build_object('level', l.level, 'minutes', l.minutes, 'points', l.points,
            'instruction', l.instruction) ORDER BY array_position(ARRAY['simple','standard','stretch'], l.level)),
          '[]'::jsonb)
   FROM hq.habit_levels l WHERE l.habit_id = v.habit_id AND l.version = v.version) AS levels,
  (SELECT coalesce(jsonb_object_agg(g.goal_id, g.strength), '{}'::jsonb)
   FROM hq.habit_goals g WHERE g.habit_id = v.habit_id AND g.version = v.version) AS goals,
  (SELECT coalesce(array_agg(t.tag ORDER BY t.tag), '{}'::text[])
   FROM hq.habit_tags t WHERE t.habit_id = v.habit_id AND t.version = v.version) AS tags,
  (SELECT coalesce(jsonb_object_agg(x.interest, jsonb_build_object('title', x.title, 'instruction', x.instruction)),
          '{}'::jsonb)
   FROM hq.habit_variants x WHERE x.habit_id = v.habit_id AND x.version = v.version) AS variants,
  (SELECT coalesce(jsonb_object_agg(p.supports_habit_id, p.kind), '{}'::jsonb)
   FROM hq.habit_prereqs p WHERE p.habit_id = v.habit_id AND p.version = v.version) AS supports
FROM hq.habit_versions v;

CREATE VIEW hq.concern_version_details AS
SELECT c.*,
  (SELECT coalesce(jsonb_object_agg(cg.goal_id, cg.weight), '{}'::jsonb) FROM hq.concern_goals cg
   WHERE cg.concern_id = c.id AND cg.concern_version = c.version) AS goals
FROM hq.concerns c;

CREATE VIEW hq.release_goals AS
SELECT i.release_id, g.id AS goal_id, g.version, g.title, g.description
FROM hq.release_items i
JOIN hq.goals g ON i.entity = 'goal' AND g.id = i.entity_id AND g.version = i.version AND g.status = 'approved';

CREATE VIEW hq.release_concerns AS
SELECT i.release_id, c.id AS concern_id, c.version, c.label, c.synonyms, c.goals
FROM hq.release_items i
JOIN hq.concern_version_details c ON i.entity = 'concern' AND c.id = i.entity_id AND c.version = i.version
  AND c.status = 'approved';

CREATE VIEW hq.release_chapters AS
SELECT i.release_id, t.id AS chapter_id, t.version, t.world, t.goal_id, t.title
FROM hq.release_items i
JOIN hq.chapter_templates t ON i.entity = 'chapter' AND t.id = i.entity_id AND t.version = i.version
  AND t.status = 'approved';

CREATE VIEW hq.release_habits AS
SELECT i.release_id, d.habit_id, d.version, d.title, d.instruction, d.min_age, d.max_age, d.routine_anchor,
  d.withdrawn_at IS NOT NULL AS withdrawn, d.levels, d.goals, d.tags, d.variants, d.supports
FROM hq.release_items i
JOIN hq.habit_version_details d ON i.entity = 'habit' AND d.habit_id = i.entity_id AND d.version = i.version
  AND d.status = 'approved';

GRANT SELECT ON hq.habit_version_details, hq.concern_version_details, hq.release_goals, hq.release_concerns,
  hq.release_chapters, hq.release_habits TO hq_editor;

-- Draft -> in_review -> approved, or back to draft on rejection. Reviewer differs from author.
CREATE FUNCTION hq.transition_version(kind text, target_id text, target_version int, action text,
  why text DEFAULT NULL) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,hq AS $$
DECLARE
  actor uuid := hq.my_editor();
  tbl text := CASE kind WHEN 'goal' THEN 'goals' WHEN 'concern' THEN 'concerns'
                        WHEN 'habit' THEN 'habit_versions' WHEN 'chapter' THEN 'chapter_templates' END;
  id_column text := CASE kind WHEN 'habit' THEN 'habit_id' ELSE 'id' END;
  current_status text;
  author uuid;
  next_status text;
BEGIN
  IF actor IS NULL THEN
    RAISE EXCEPTION 'Editor identity required' USING ERRCODE = '42501';
  END IF;
  IF tbl IS NULL OR action NOT IN ('submit','approve','reject') THEN
    RAISE EXCEPTION 'Unknown catalog entity or action' USING ERRCODE = '22023';
  END IF;
  EXECUTE format('SELECT status, created_by FROM hq.%I WHERE %I = $1 AND version = $2 FOR UPDATE', tbl, id_column)
    INTO current_status, author USING target_id, target_version;
  IF current_status IS NULL THEN
    RAISE EXCEPTION 'Catalog version not found' USING ERRCODE = 'P0002';
  END IF;
  IF action = 'submit' THEN
    IF current_status <> 'draft' THEN
      RAISE EXCEPTION 'Only drafts can be submitted' USING ERRCODE = '55000';
    END IF;
    next_status := 'in_review';
  ELSE
    IF current_status <> 'in_review' THEN
      RAISE EXCEPTION 'Only versions in review can be approved or rejected' USING ERRCODE = '55000';
    END IF;
    IF author = actor THEN
      RAISE EXCEPTION 'Reviewer must differ from author' USING ERRCODE = '42501';
    END IF;
    IF action = 'reject' AND coalesce(btrim(why), '') = '' THEN
      RAISE EXCEPTION 'Rejection requires a reason' USING ERRCODE = '22023';
    END IF;
    next_status := CASE action WHEN 'approve' THEN 'approved' ELSE 'draft' END;
  END IF;
  IF next_status = 'approved' THEN
    EXECUTE format('UPDATE hq.%I SET status = $1, reviewed_by = $2, reviewed_at = now() '
                   'WHERE %I = $3 AND version = $4', tbl, id_column)
      USING next_status, actor, target_id, target_version;
  ELSE
    EXECUTE format('UPDATE hq.%I SET status = $1 WHERE %I = $2 AND version = $3', tbl, id_column)
      USING next_status, target_id, target_version;
  END IF;
  INSERT INTO hq.catalog_audit(entity, entity_id, version, from_status, to_status, actor, reason)
  VALUES (kind, target_id, target_version, current_status, next_status, actor, why);
  RETURN next_status;
END $$;

-- A release candidate holds the latest approved, non-withdrawn version of every entity,
-- or (rollback) a published release's items minus withdrawn habit versions.
CREATE FUNCTION hq.build_release(release_notes text, source_release int DEFAULT NULL) RETURNS int
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,hq AS $$
DECLARE
  actor uuid := hq.my_editor();
  new_id int;
BEGIN
  IF actor IS NULL THEN
    RAISE EXCEPTION 'Editor identity required' USING ERRCODE = '42501';
  END IF;
  PERFORM pg_advisory_xact_lock(29102026);
  IF source_release IS NOT NULL
     AND NOT EXISTS(SELECT 1 FROM hq.catalog_releases WHERE id = source_release AND published_at IS NOT NULL) THEN
    RAISE EXCEPTION 'Source release is not published' USING ERRCODE = 'P0002';
  END IF;
  SELECT coalesce(max(id), 0) + 1 INTO new_id FROM hq.catalog_releases;
  INSERT INTO hq.catalog_releases(id, built_by, notes) VALUES (new_id, actor, coalesce(release_notes, ''));
  IF source_release IS NULL THEN
    INSERT INTO hq.release_items
    SELECT new_id, 'goal', id, max(version) FROM hq.goals WHERE status = 'approved' GROUP BY id
    UNION ALL SELECT new_id, 'concern', id, max(version) FROM hq.concerns WHERE status = 'approved' GROUP BY id
    UNION ALL SELECT new_id, 'habit', habit_id, max(version) FROM hq.habit_versions
      WHERE status = 'approved' AND withdrawn_at IS NULL GROUP BY habit_id
    UNION ALL SELECT new_id, 'chapter', id, max(version) FROM hq.chapter_templates
      WHERE status = 'approved' GROUP BY id;
  ELSE
    INSERT INTO hq.release_items
    SELECT new_id, i.entity, i.entity_id, i.version FROM hq.release_items i
    WHERE i.release_id = source_release AND NOT EXISTS(
      SELECT 1 FROM hq.habit_versions v WHERE i.entity = 'habit' AND v.habit_id = i.entity_id
        AND v.version = i.version AND v.withdrawn_at IS NOT NULL);
  END IF;
  RETURN new_id;
END $$;

CREATE FUNCTION hq.publish_release(target int) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,hq AS $$
DECLARE
  actor uuid := hq.my_editor();
  builder uuid;
  published timestamptz;
BEGIN
  IF actor IS NULL THEN
    RAISE EXCEPTION 'Editor identity required' USING ERRCODE = '42501';
  END IF;
  PERFORM pg_advisory_xact_lock(29102026);
  SELECT built_by, published_at INTO builder, published FROM hq.catalog_releases WHERE id = target FOR UPDATE;
  IF builder IS NULL THEN
    RAISE EXCEPTION 'Release not found' USING ERRCODE = 'P0002';
  END IF;
  IF published IS NOT NULL THEN
    RAISE EXCEPTION 'Release is already published' USING ERRCODE = '55000';
  END IF;
  IF builder = actor THEN
    RAISE EXCEPTION 'Publisher must differ from release builder' USING ERRCODE = '42501';
  END IF;
  UPDATE hq.catalog_releases SET is_current = false WHERE is_current;
  UPDATE hq.catalog_releases SET published_by = actor, published_at = now(), is_current = true WHERE id = target;
END $$;

-- Permanent withdrawal of every approved version; flags each child's current plan that uses the habit.
-- Open proposals already fail confirmation through release revalidation.
CREATE FUNCTION hq.withdraw_habit(target text, why text) RETURNS int
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,hq AS $$
DECLARE
  actor uuid := hq.my_editor();
  withdrawn int;
  flagged int;
BEGIN
  IF actor IS NULL THEN
    RAISE EXCEPTION 'Editor identity required' USING ERRCODE = '42501';
  END IF;
  IF coalesce(btrim(why), '') = '' THEN
    RAISE EXCEPTION 'Withdrawal requires a reason' USING ERRCODE = '22023';
  END IF;
  WITH changed AS (
    UPDATE hq.habit_versions SET withdrawn_at = now(), withdrawn_reason = why
    WHERE habit_id = target AND status = 'approved' AND withdrawn_at IS NULL RETURNING version)
  INSERT INTO hq.catalog_audit(entity, entity_id, version, from_status, to_status, actor, reason)
  SELECT 'habit', target, version, 'approved', 'withdrawn', actor, why FROM changed;
  GET DIAGNOSTICS withdrawn = ROW_COUNT;
  IF withdrawn = 0 THEN
    IF EXISTS(SELECT 1 FROM hq.habit_versions WHERE habit_id = target AND status = 'approved') THEN
      RAISE EXCEPTION 'Habit is already withdrawn' USING ERRCODE = '55000';
    END IF;
    RAISE EXCEPTION 'No approved version of this habit' USING ERRCODE = 'P0002';
  END IF;
  INSERT INTO hq.plan_flags(plan_id, kind, habit_id, reason)
  SELECT p.id, 'habit_withdrawn', target, 'This activity was withdrawn from the catalog; review the plan.'
  FROM (SELECT DISTINCT ON (child_id) id, data FROM hq.plans ORDER BY child_id, version DESC) p
  WHERE EXISTS(SELECT 1 FROM jsonb_array_elements(p.data->'active') h WHERE h->>'habit_id' = target);
  GET DIAGNOSTICS flagged = ROW_COUNT;
  RETURN flagged;
END $$;

REVOKE ALL ON FUNCTION hq.transition_version(text, text, int, text, text), hq.build_release(text, int),
  hq.publish_release(int), hq.withdraw_habit(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION hq.transition_version(text, text, int, text, text), hq.build_release(text, int),
  hq.publish_release(int), hq.withdraw_habit(text, text) TO hq_editor;
