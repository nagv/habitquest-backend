-- Versioned, two-person-reviewed habit catalog with numbered releases.
-- Coexists with hq.catalog/hq.chapters until planning switches over (plan 2).

CREATE FUNCTION hq.guard_approved_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.status <> 'approved' THEN
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
  END IF;
  -- Withdrawal is the only change allowed after approval.
  IF TG_OP = 'DELETE' OR (to_jsonb(NEW) - 'withdrawn_at' - 'withdrawn_reason')
       IS DISTINCT FROM (to_jsonb(OLD) - 'withdrawn_at' - 'withdrawn_reason') THEN
    RAISE EXCEPTION 'Approved catalog versions are immutable' USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;

CREATE FUNCTION hq.guard_habit_detail() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP <> 'INSERT' AND EXISTS(SELECT 1 FROM hq.habit_versions v
      WHERE v.habit_id = OLD.habit_id AND v.version = OLD.version AND v.status = 'approved') THEN
    RAISE EXCEPTION 'Approved catalog versions are immutable' USING ERRCODE = '42501';
  END IF;
  IF TG_OP <> 'DELETE' AND EXISTS(SELECT 1 FROM hq.habit_versions v
      WHERE v.habit_id = NEW.habit_id AND v.version = NEW.version AND v.status = 'approved') THEN
    RAISE EXCEPTION 'Approved catalog versions are immutable' USING ERRCODE = '42501';
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;

CREATE FUNCTION hq.guard_concern_detail() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP <> 'INSERT' AND EXISTS(SELECT 1 FROM hq.concerns c
      WHERE c.id = OLD.concern_id AND c.version = OLD.concern_version AND c.status = 'approved') THEN
    RAISE EXCEPTION 'Approved catalog versions are immutable' USING ERRCODE = '42501';
  END IF;
  IF TG_OP <> 'DELETE' AND EXISTS(SELECT 1 FROM hq.concerns c
      WHERE c.id = NEW.concern_id AND c.version = NEW.concern_version AND c.status = 'approved') THEN
    RAISE EXCEPTION 'Approved catalog versions are immutable' USING ERRCODE = '42501';
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;

CREATE FUNCTION hq.guard_release_items() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP <> 'INSERT' AND EXISTS(SELECT 1 FROM hq.catalog_releases r
      WHERE r.id = OLD.release_id AND r.published_at IS NOT NULL) THEN
    RAISE EXCEPTION 'Published releases are immutable' USING ERRCODE = '42501';
  END IF;
  IF TG_OP <> 'DELETE' AND EXISTS(SELECT 1 FROM hq.catalog_releases r
      WHERE r.id = NEW.release_id AND r.published_at IS NOT NULL) THEN
    RAISE EXCEPTION 'Published releases are immutable' USING ERRCODE = '42501';
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;

CREATE TABLE hq.goals(
  id text NOT NULL CHECK(id ~ '^[a-z][a-z0-9-]*$'), version int NOT NULL CHECK(version > 0),
  title text NOT NULL, description text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','in_review','approved','rejected')),
  created_by uuid NOT NULL, reviewed_by uuid, reviewed_at timestamptz,
  CHECK(status <> 'approved' OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL AND reviewed_by <> created_by)),
  PRIMARY KEY(id, version));

CREATE TABLE hq.concerns(
  id text NOT NULL CHECK(id ~ '^[a-z][a-z0-9-]*$'), version int NOT NULL CHECK(version > 0),
  label text NOT NULL, synonyms text[] NOT NULL DEFAULT '{}',
  status text NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','in_review','approved','rejected')),
  created_by uuid NOT NULL, reviewed_by uuid, reviewed_at timestamptz,
  CHECK(status <> 'approved' OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL AND reviewed_by <> created_by)),
  PRIMARY KEY(id, version));

CREATE TABLE hq.concern_goals(
  concern_id text NOT NULL, concern_version int NOT NULL, goal_id text NOT NULL,
  weight numeric(3,2) NOT NULL CHECK(weight > 0 AND weight <= 1),
  PRIMARY KEY(concern_id, concern_version, goal_id),
  FOREIGN KEY(concern_id, concern_version) REFERENCES hq.concerns ON DELETE CASCADE);

CREATE TABLE hq.habits(id text PRIMARY KEY CHECK(id ~ '^[a-z][a-z0-9-]*$'));

CREATE TABLE hq.habit_versions(
  habit_id text NOT NULL REFERENCES hq.habits, version int NOT NULL CHECK(version > 0),
  title text NOT NULL, instruction text NOT NULL,
  min_age int NOT NULL CHECK(min_age BETWEEN 6 AND 13), max_age int NOT NULL CHECK(max_age BETWEEN 6 AND 13),
  routine_anchor text NOT NULL CHECK(routine_anchor IN ('morning','after_school','evening','bedtime')),
  safety_notes text NOT NULL DEFAULT '', withdrawn_at timestamptz, withdrawn_reason text,
  status text NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','in_review','approved','rejected')),
  created_by uuid NOT NULL, reviewed_by uuid, reviewed_at timestamptz,
  CHECK(status <> 'approved' OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL AND reviewed_by <> created_by)),
  CHECK(min_age <= max_age),
  CHECK((withdrawn_at IS NULL) = (withdrawn_reason IS NULL)),
  PRIMARY KEY(habit_id, version));

CREATE TABLE hq.habit_levels(
  habit_id text NOT NULL, version int NOT NULL,
  level text NOT NULL CHECK(level IN ('simple','standard','stretch')),
  minutes int NOT NULL CHECK(minutes BETWEEN 1 AND 60), points int NOT NULL CHECK(points BETWEEN 1 AND 3),
  instruction text,
  PRIMARY KEY(habit_id, version, level),
  FOREIGN KEY(habit_id, version) REFERENCES hq.habit_versions ON DELETE CASCADE);

CREATE TABLE hq.habit_goals(
  habit_id text NOT NULL, version int NOT NULL, goal_id text NOT NULL,
  strength numeric(3,2) NOT NULL CHECK(strength > 0 AND strength <= 1),
  PRIMARY KEY(habit_id, version, goal_id),
  FOREIGN KEY(habit_id, version) REFERENCES hq.habit_versions ON DELETE CASCADE);

CREATE TABLE hq.habit_prereqs(
  habit_id text NOT NULL, version int NOT NULL, supports_habit_id text NOT NULL REFERENCES hq.habits,
  kind text NOT NULL CHECK(kind IN ('prepares')),
  CHECK(supports_habit_id <> habit_id),
  PRIMARY KEY(habit_id, version, supports_habit_id),
  FOREIGN KEY(habit_id, version) REFERENCES hq.habit_versions ON DELETE CASCADE);

CREATE TABLE hq.habit_tags(
  habit_id text NOT NULL, version int NOT NULL,
  tag text NOT NULL CHECK(tag ~ '^(interest|topic|access):[a-z_]+$'),
  PRIMARY KEY(habit_id, version, tag),
  FOREIGN KEY(habit_id, version) REFERENCES hq.habit_versions ON DELETE CASCADE);

CREATE TABLE hq.habit_variants(
  habit_id text NOT NULL, version int NOT NULL, interest text NOT NULL CHECK(interest ~ '^[a-z_]+$'),
  title text NOT NULL, instruction text NOT NULL,
  PRIMARY KEY(habit_id, version, interest),
  FOREIGN KEY(habit_id, version) REFERENCES hq.habit_versions ON DELETE CASCADE);

CREATE TABLE hq.chapter_templates(
  id text NOT NULL CHECK(id ~ '^[a-z][a-z0-9-]*$'), version int NOT NULL CHECK(version > 0),
  world text NOT NULL CHECK(world IN ('space','forest','city')), goal_id text NOT NULL, title text NOT NULL,
  status text NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','in_review','approved','rejected')),
  created_by uuid NOT NULL, reviewed_by uuid, reviewed_at timestamptz,
  CHECK(status <> 'approved' OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL AND reviewed_by <> created_by)),
  PRIMARY KEY(id, version));

CREATE TABLE hq.catalog_releases(
  id int PRIMARY KEY CHECK(id > 0), built_by uuid NOT NULL, published_by uuid, published_at timestamptz,
  notes text NOT NULL DEFAULT '', is_current boolean NOT NULL DEFAULT false,
  CHECK((published_by IS NULL) = (published_at IS NULL)),
  CHECK(published_by IS NULL OR published_by <> built_by),
  CHECK(NOT is_current OR published_at IS NOT NULL));
CREATE UNIQUE INDEX one_current_release ON hq.catalog_releases(is_current) WHERE is_current;

CREATE TABLE hq.release_items(
  release_id int NOT NULL REFERENCES hq.catalog_releases,
  entity text NOT NULL CHECK(entity IN ('goal','concern','habit','chapter')),
  entity_id text NOT NULL, version int NOT NULL,
  PRIMARY KEY(release_id, entity, entity_id));

CREATE TRIGGER immutable_approved BEFORE UPDATE OR DELETE ON hq.goals
  FOR EACH ROW EXECUTE FUNCTION hq.guard_approved_version();
CREATE TRIGGER immutable_approved BEFORE UPDATE OR DELETE ON hq.concerns
  FOR EACH ROW EXECUTE FUNCTION hq.guard_approved_version();
CREATE TRIGGER immutable_approved BEFORE UPDATE OR DELETE ON hq.habit_versions
  FOR EACH ROW EXECUTE FUNCTION hq.guard_approved_version();
CREATE TRIGGER immutable_approved BEFORE UPDATE OR DELETE ON hq.chapter_templates
  FOR EACH ROW EXECUTE FUNCTION hq.guard_approved_version();
CREATE TRIGGER immutable_approved BEFORE INSERT OR UPDATE OR DELETE ON hq.concern_goals
  FOR EACH ROW EXECUTE FUNCTION hq.guard_concern_detail();
CREATE TRIGGER immutable_approved BEFORE INSERT OR UPDATE OR DELETE ON hq.release_items
  FOR EACH ROW EXECUTE FUNCTION hq.guard_release_items();
DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['habit_levels','habit_goals','habit_prereqs','habit_tags','habit_variants'] LOOP
    EXECUTE format('CREATE TRIGGER immutable_approved BEFORE INSERT OR UPDATE OR DELETE ON hq.%I '
                   'FOR EACH ROW EXECUTE FUNCTION hq.guard_habit_detail()', t);
  END LOOP;
END $$;

-- Service roles read only published, approved content through these views.
CREATE VIEW hq.current_release AS SELECT id FROM hq.catalog_releases WHERE is_current;

CREATE VIEW hq.released_goals AS
SELECT i.release_id, g.id AS goal_id, g.version, g.title, g.description
FROM hq.release_items i
JOIN hq.catalog_releases r ON r.id = i.release_id AND r.published_at IS NOT NULL
JOIN hq.goals g ON i.entity = 'goal' AND g.id = i.entity_id AND g.version = i.version AND g.status = 'approved';

CREATE VIEW hq.released_concerns AS
SELECT i.release_id, c.id AS concern_id, c.version, c.label, c.synonyms,
  (SELECT coalesce(jsonb_object_agg(cg.goal_id, cg.weight), '{}'::jsonb) FROM hq.concern_goals cg
   WHERE cg.concern_id = c.id AND cg.concern_version = c.version) AS goals
FROM hq.release_items i
JOIN hq.catalog_releases r ON r.id = i.release_id AND r.published_at IS NOT NULL
JOIN hq.concerns c ON i.entity = 'concern' AND c.id = i.entity_id AND c.version = i.version AND c.status = 'approved';

CREATE VIEW hq.released_chapters AS
SELECT i.release_id, t.id AS chapter_id, t.version, t.world, t.goal_id, t.title
FROM hq.release_items i
JOIN hq.catalog_releases r ON r.id = i.release_id AND r.published_at IS NOT NULL
JOIN hq.chapter_templates t ON i.entity = 'chapter' AND t.id = i.entity_id AND t.version = i.version
  AND t.status = 'approved';

CREATE VIEW hq.released_habits AS
SELECT i.release_id, v.habit_id, v.version, v.title, v.instruction, v.min_age, v.max_age, v.routine_anchor,
  v.withdrawn_at IS NOT NULL AS withdrawn,
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
FROM hq.release_items i
JOIN hq.catalog_releases r ON r.id = i.release_id AND r.published_at IS NOT NULL
JOIN hq.habit_versions v ON i.entity = 'habit' AND v.habit_id = i.entity_id AND v.version = i.version
  AND v.status = 'approved';

GRANT SELECT ON hq.current_release, hq.released_goals, hq.released_concerns, hq.released_chapters,
  hq.released_habits TO hq_api, hq_worker;
