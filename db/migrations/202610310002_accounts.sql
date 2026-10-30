-- Accounts: Google users, creating and joining families, and kid sessions.
-- The functions below are SECURITY DEFINER because each one acts before the caller can see the rows
-- involved: a new Google user has no id yet, and a family's first or joining caregiver is not yet a member.

CREATE FUNCTION hq.google_user(p_sub text, p_email text, p_name text) RETURNS uuid
  LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  INSERT INTO hq.users(google_sub, email, name) VALUES (p_sub, p_email, p_name)
  ON CONFLICT (google_sub) DO UPDATE SET email = EXCLUDED.email, name = EXCLUDED.name
  RETURNING id
$$;

CREATE FUNCTION hq.create_family(
  p_name text, p_timezone text, p_pin_hash text, p_invite_code text, p_display_name text, p_role text
) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE family uuid;
BEGIN
  IF hq.my_user() IS NULL THEN
    RAISE EXCEPTION 'sign in to create a family' USING ERRCODE = 'insufficient_privilege';
  END IF;
  INSERT INTO hq.families(name, timezone, pin_hash, invite_code)
    VALUES (p_name, p_timezone, p_pin_hash, p_invite_code) RETURNING id INTO family;
  INSERT INTO hq.caregivers(family_id, user_id, display_name, role)
    VALUES (family, hq.my_user(), p_display_name, p_role);
  RETURN family;
END $$;

CREATE FUNCTION hq.join_family(p_invite_code text, p_display_name text, p_role text) RETURNS uuid
  LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE family uuid;
BEGIN
  IF hq.my_user() IS NULL THEN
    RAISE EXCEPTION 'sign in to join a family' USING ERRCODE = 'insufficient_privilege';
  END IF;
  SELECT id INTO family FROM hq.families WHERE invite_code = p_invite_code;
  IF family IS NULL THEN
    RAISE EXCEPTION 'unknown invite code' USING ERRCODE = 'no_data_found';
  END IF;
  INSERT INTO hq.caregivers(family_id, user_id, display_name, role)
    VALUES (family, hq.my_user(), p_display_name, p_role);
  RETURN family;
END $$;

REVOKE ALL ON FUNCTION hq.google_user(text, text, text), hq.create_family(text, text, text, text, text, text),
  hq.join_family(text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION hq.google_user(text, text, text), hq.create_family(text, text, text, text, text, text),
  hq.join_family(text, text, text) TO hq_api;

-- Kid sessions run as hq_kid with app.child_id set; they see only their own child row for now.
-- (The daily loop adds the kid's habits and completions.)
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'hq_kid') THEN CREATE ROLE hq_kid NOLOGIN; END IF;
END $$;
GRANT USAGE ON SCHEMA hq TO hq_kid;

CREATE FUNCTION hq.my_kid() RETURNS uuid LANGUAGE sql STABLE AS
  $$ SELECT NULLIF(current_setting('app.child_id', true), '')::uuid $$;

-- The family policies are for caregivers' requests only; kid policies are separate and narrower.
ALTER POLICY own_user ON hq.users TO hq_api;
ALTER POLICY my_family ON hq.families TO hq_api;
ALTER POLICY my_family ON hq.caregivers TO hq_api;
ALTER POLICY my_family ON hq.children TO hq_api;
ALTER POLICY my_children ON hq.pain_points TO hq_api;
ALTER POLICY my_children ON hq.child_goals TO hq_api;
ALTER POLICY my_children ON hq.child_habits TO hq_api;
ALTER POLICY my_children ON hq.points_ledger TO hq_api;
ALTER POLICY my_children ON hq.completions TO hq_api;

CREATE POLICY kid_self ON hq.children FOR SELECT TO hq_kid USING (id = hq.my_kid());
GRANT SELECT ON hq.children TO hq_kid;
