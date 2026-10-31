-- The latest quest plan and weekly review shown on the dashboard, and the daily AI budget for kid tokens.

CREATE TABLE hq.ai_reports(
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  family_id uuid NOT NULL REFERENCES hq.families ON DELETE CASCADE,
  kind text NOT NULL CHECK (kind IN ('plan', 'review')),
  result jsonb NOT NULL CHECK (jsonb_typeof(result) = 'object'),
  created_by uuid NOT NULL REFERENCES hq.users,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ai_reports_family ON hq.ai_reports(family_id, kind, created_at DESC);
ALTER TABLE hq.ai_reports ENABLE ROW LEVEL SECURITY;
CREATE POLICY my_family ON hq.ai_reports TO hq_api USING (family_id IN (SELECT hq.my_families()));
GRANT SELECT, INSERT ON hq.ai_reports TO hq_api;

-- Kids cannot touch ai_usage. SECURITY DEFINER so a kid token can count a call for its own family, and
-- nothing else; it returns only the new count.
CREATE FUNCTION hq.kid_ai_call() RETURNS int LANGUAGE sql SECURITY DEFINER
  SET search_path = pg_catalog, pg_temp AS $$
  INSERT INTO hq.ai_usage(family_id, day, calls)
  SELECT family_id, current_date, 1 FROM hq.children WHERE id = NULLIF(current_setting('app.child_id', true), '')::uuid
  ON CONFLICT (family_id, day) DO UPDATE SET calls = hq.ai_usage.calls + 1
  RETURNING calls
$$;
REVOKE ALL ON FUNCTION hq.kid_ai_call() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION hq.kid_ai_call() TO hq_kid;
