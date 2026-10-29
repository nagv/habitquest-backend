-- Hardens the catalog guards: frozen published releases, no un-withdrawal,
-- and parent rows locked while details are checked (closes approve/publish races).

CREATE FUNCTION hq.guard_published_release() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.published_at IS NULL THEN
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
  END IF;
  -- Only the current flag and notes may change after publication.
  IF TG_OP = 'DELETE' OR (to_jsonb(NEW) - 'is_current' - 'notes')
       IS DISTINCT FROM (to_jsonb(OLD) - 'is_current' - 'notes') THEN
    RAISE EXCEPTION 'Published releases are immutable' USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER frozen_published BEFORE UPDATE OR DELETE ON hq.catalog_releases
  FOR EACH ROW EXECUTE FUNCTION hq.guard_published_release();

CREATE OR REPLACE FUNCTION hq.guard_approved_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.status <> 'approved' THEN
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
  END IF;
  -- Withdrawal is the only change allowed after approval, and it is permanent.
  IF TG_OP = 'DELETE' OR (to_jsonb(NEW) - 'withdrawn_at' - 'withdrawn_reason')
       IS DISTINCT FROM (to_jsonb(OLD) - 'withdrawn_at' - 'withdrawn_reason') THEN
    RAISE EXCEPTION 'Approved catalog versions are immutable' USING ERRCODE = '42501';
  END IF;
  IF to_jsonb(OLD)->>'withdrawn_at' IS NOT NULL AND (
       to_jsonb(NEW)->'withdrawn_at' IS DISTINCT FROM to_jsonb(OLD)->'withdrawn_at'
       OR to_jsonb(NEW)->'withdrawn_reason' IS DISTINCT FROM to_jsonb(OLD)->'withdrawn_reason') THEN
    RAISE EXCEPTION 'Withdrawn catalog versions cannot be changed' USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;

-- Detail guards lock the parent row before checking its status.
CREATE OR REPLACE FUNCTION hq.guard_habit_detail() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE parent_status text;
BEGIN
  IF TG_OP <> 'INSERT' THEN
    SELECT v.status INTO parent_status FROM hq.habit_versions v
      WHERE v.habit_id = OLD.habit_id AND v.version = OLD.version FOR SHARE;
    IF parent_status = 'approved' THEN
      RAISE EXCEPTION 'Approved catalog versions are immutable' USING ERRCODE = '42501';
    END IF;
  END IF;
  IF TG_OP <> 'DELETE' THEN
    SELECT v.status INTO parent_status FROM hq.habit_versions v
      WHERE v.habit_id = NEW.habit_id AND v.version = NEW.version FOR SHARE;
    IF parent_status = 'approved' THEN
      RAISE EXCEPTION 'Approved catalog versions are immutable' USING ERRCODE = '42501';
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
    END IF;
  END IF;
  IF TG_OP <> 'DELETE' THEN
    SELECT c.status INTO parent_status FROM hq.concerns c
      WHERE c.id = NEW.concern_id AND c.version = NEW.concern_version FOR SHARE;
    IF parent_status = 'approved' THEN
      RAISE EXCEPTION 'Approved catalog versions are immutable' USING ERRCODE = '42501';
    END IF;
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION hq.guard_release_items() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE published timestamptz;
BEGIN
  IF TG_OP <> 'INSERT' THEN
    SELECT r.published_at INTO published FROM hq.catalog_releases r WHERE r.id = OLD.release_id FOR SHARE;
    IF published IS NOT NULL THEN
      RAISE EXCEPTION 'Published releases are immutable' USING ERRCODE = '42501';
    END IF;
  END IF;
  IF TG_OP <> 'DELETE' THEN
    SELECT r.published_at INTO published FROM hq.catalog_releases r WHERE r.id = NEW.release_id FOR SHARE;
    IF published IS NOT NULL THEN
      RAISE EXCEPTION 'Published releases are immutable' USING ERRCODE = '42501';
    END IF;
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;
