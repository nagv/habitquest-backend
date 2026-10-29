-- Switch onboarding to durable profiles and repeatable intakes; retire the pre-release catalog (plan 2a).

-- Durable profile values replace plan-derived preference rows.
UPDATE hq.children c SET preferred_world = p.value #>> '{}'
FROM hq.preferences p
WHERE p.child_id = c.id AND p.kind = 'world' AND p.value #>> '{}' IN ('space','forest','city');
UPDATE hq.children c SET routine_schedule = (
  SELECT coalesce(jsonb_object_agg(e.key, jsonb_build_object('cue', e.value #>> '{}',
         'days', '["mon","tue","wed","thu","fri","sat","sun"]'::jsonb)), '{}'::jsonb)
  FROM jsonb_each(p.value) e)
FROM hq.preferences p
WHERE p.child_id = c.id AND p.kind = 'routine_times' AND jsonb_typeof(p.value) = 'object';
UPDATE hq.households h SET timezone = latest.data->>'timezone'
FROM (SELECT DISTINCT ON (c.household_id) c.household_id, s.data
      FROM hq.sessions s JOIN hq.children c ON c.id = s.child_id
      ORDER BY c.household_id, s.version DESC) latest
WHERE h.id = latest.household_id AND latest.data ? 'timezone';
DROP TABLE hq.preferences;

ALTER TABLE hq.sessions RENAME TO intakes;
ALTER TABLE hq.intakes DROP COLUMN goals_confirmed;
ALTER TABLE hq.intakes ADD COLUMN created_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE hq.jobs RENAME COLUMN session_id TO intake_id;
ALTER TABLE hq.jobs RENAME COLUMN session_version TO intake_version;
ALTER TABLE hq.proposals RENAME COLUMN session_version TO intake_version;

-- Analysis jobs and proposals built from the retired catalog can no longer be used.
UPDATE hq.jobs SET status = 'cancelled', lease_token = NULL, lease_until = NULL
WHERE kind = 'analysis' AND status IN ('queued','running');
UPDATE hq.proposals SET expires_at = now() WHERE NOT confirmed AND release_id IS NULL;

DROP FUNCTION hq.lock_sources(text[], text[]);
DROP TABLE hq.catalog, hq.chapters;

DROP FUNCTION hq.lock_session(uuid);
CREATE FUNCTION hq.lock_intake(intake_id uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,hq AS $$
BEGIN
  PERFORM 1 FROM hq.intakes s WHERE s.id = intake_id AND s.parent_id = hq.my_parent()
    AND EXISTS(SELECT 1 FROM hq.children c JOIN hq.memberships m ON m.household_id = c.household_id
               WHERE c.id = s.child_id AND m.parent_id = hq.my_parent()) FOR SHARE;
END $$;
REVOKE ALL ON FUNCTION hq.lock_intake(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION hq.lock_intake(uuid) TO hq_worker;
