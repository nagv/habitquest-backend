-- The daily loop: "today" in the family's time zone, and what a kid token may read and write.

-- The child's current date in their family's time zone. SECURITY DEFINER because kids cannot read families;
-- it returns only a date.
CREATE FUNCTION hq.child_today(p_child uuid) RETURNS date LANGUAGE sql STABLE SECURITY DEFINER
  SET search_path = pg_catalog, pg_temp AS $$
  SELECT (now() AT TIME ZONE f.timezone)::date
  FROM hq.children c JOIN hq.families f ON f.id = c.family_id
  WHERE c.id = p_child
$$;
REVOKE ALL ON FUNCTION hq.child_today(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION hq.child_today(uuid) TO hq_api, hq_kid;

-- A kid reads the catalog, their own habits, completions and points, and submits today's completions:
-- a new one, or a rejected one again. Only caregivers approve or reject.
GRANT SELECT ON hq.goals, hq.habits, hq.child_habits, hq.completions, hq.points_ledger TO hq_kid;
GRANT INSERT ON hq.completions TO hq_kid;
GRANT UPDATE (status, submitted_at, decided_by, decided_at) ON hq.completions TO hq_kid;

CREATE POLICY kid_self ON hq.child_habits FOR SELECT TO hq_kid USING (child_id = hq.my_kid());
CREATE POLICY kid_self ON hq.points_ledger FOR SELECT TO hq_kid USING (child_id = hq.my_kid());
CREATE POLICY kid_read ON hq.completions FOR SELECT TO hq_kid
  USING (child_habit_id IN (SELECT id FROM hq.child_habits));
CREATE POLICY kid_submit ON hq.completions FOR INSERT TO hq_kid WITH CHECK (
  status = 'submitted' AND decided_by IS NULL AND day = hq.child_today(hq.my_kid())
  AND child_habit_id IN (SELECT id FROM hq.child_habits WHERE status = 'active')
);
CREATE POLICY kid_resubmit ON hq.completions FOR UPDATE TO hq_kid
  USING (status = 'rejected' AND child_habit_id IN (SELECT id FROM hq.child_habits WHERE status = 'active'))
  WITH CHECK (status = 'submitted' AND decided_by IS NULL AND day = hq.child_today(hq.my_kid()));
