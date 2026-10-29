-- Additive schema for child profiles, goal tracking and catalog-backed proposals (plan 2a).

ALTER TABLE hq.children
  ADD COLUMN interests text[] NOT NULL DEFAULT '{}',
  ADD COLUMN accessibility text[] NOT NULL DEFAULT '{}',
  ADD COLUMN opted_in_topics text[] NOT NULL DEFAULT '{}',
  ADD COLUMN preferred_world text NOT NULL DEFAULT 'space' CHECK(preferred_world IN ('space','forest','city')),
  ADD COLUMN routine_schedule jsonb NOT NULL DEFAULT '{}',
  ADD COLUMN profile_version int NOT NULL DEFAULT 1 CHECK(profile_version > 0);
GRANT UPDATE(name, age_band, interests, accessibility, opted_in_topics, preferred_world, routine_schedule,
  profile_version) ON hq.children TO hq_api;
GRANT UPDATE(timezone) ON hq.households TO hq_api;

CREATE TABLE hq.child_goals(
  child_id uuid NOT NULL REFERENCES hq.children, parent_id uuid NOT NULL, goal_id text NOT NULL,
  status text NOT NULL CHECK(status IN ('active','paused','resolved')),
  since_plan_version int NOT NULL, source_intake_id uuid NOT NULL REFERENCES hq.sessions,
  PRIMARY KEY(child_id, goal_id));
ALTER TABLE hq.child_goals ENABLE ROW LEVEL SECURITY;
CREATE POLICY parent_scope ON hq.child_goals
  USING(parent_id = hq.my_parent() AND child_id IN (SELECT id FROM hq.children));
GRANT SELECT, INSERT, UPDATE ON hq.child_goals TO hq_api;

CREATE VIEW hq.published_releases AS SELECT id FROM hq.catalog_releases WHERE published_at IS NOT NULL;
GRANT SELECT ON hq.published_releases TO hq_api, hq_worker;

ALTER TABLE hq.proposals
  ADD COLUMN release_id int REFERENCES hq.catalog_releases,
  ADD COLUMN version int NOT NULL DEFAULT 1 CHECK(version > 0);
-- NULL release_id marks a plan built from the pre-release catalog.
ALTER TABLE hq.plans ADD COLUMN release_id int REFERENCES hq.catalog_releases;

-- Lock-only helper: holds the onboarding policy, the current release and the referenced habit
-- versions until commit, so publication or withdrawal cannot interleave with a revalidation.
CREATE FUNCTION hq.lock_release(habit_ids text[]) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,hq AS $$
BEGIN
  PERFORM 1 FROM hq.policy WHERE name = 'onboarding' FOR SHARE;
  PERFORM 1 FROM hq.catalog_releases WHERE is_current FOR SHARE;
  PERFORM 1 FROM hq.habit_versions v
    JOIN hq.release_items i ON i.entity = 'habit' AND i.entity_id = v.habit_id AND i.version = v.version
    JOIN hq.catalog_releases r ON r.id = i.release_id AND r.is_current
    WHERE v.habit_id = ANY(habit_ids) ORDER BY v.habit_id FOR SHARE OF v;
END $$;
REVOKE ALL ON FUNCTION hq.lock_release(text[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION hq.lock_release(text[]) TO hq_api, hq_worker;
