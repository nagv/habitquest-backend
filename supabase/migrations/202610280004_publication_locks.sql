ALTER TABLE hq.jobs ADD COLUMN checkpoint_thread text;
ALTER TABLE hq.jobs ADD COLUMN resume_thread text;
ALTER TABLE hq.jobs ADD COLUMN deadline timestamptz;
ALTER POLICY own_membership ON hq.memberships
  USING(parent_id=hq.my_parent())
  WITH CHECK(parent_id=hq.my_parent() AND household_id=hq.my_parent());

-- These helpers only acquire locks. They confer no catalog or session write
-- permission and have a fixed search_path to prevent object substitution.
CREATE FUNCTION hq.lock_sources(activity_ids text[],chapter_ids text[]) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,hq AS $$
BEGIN
  PERFORM 1 FROM hq.policy WHERE name='onboarding' FOR SHARE;
  PERFORM 1 FROM hq.catalog WHERE id=ANY(activity_ids) ORDER BY id FOR SHARE;
  PERFORM 1 FROM hq.chapters WHERE id=ANY(chapter_ids) ORDER BY id FOR SHARE;
END $$;
REVOKE ALL ON FUNCTION hq.lock_sources(text[],text[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION hq.lock_sources(text[],text[]) TO hq_api,hq_worker;

CREATE FUNCTION hq.lock_session(session_id uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,hq AS $$
BEGIN
  PERFORM 1 FROM hq.sessions s WHERE s.id=session_id AND s.parent_id=hq.my_parent()
    AND EXISTS(SELECT 1 FROM hq.children c JOIN hq.memberships m ON m.household_id=c.household_id
               WHERE c.id=s.child_id AND m.parent_id=hq.my_parent()) FOR SHARE;
END $$;
REVOKE ALL ON FUNCTION hq.lock_session(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION hq.lock_session(uuid) TO hq_worker;
