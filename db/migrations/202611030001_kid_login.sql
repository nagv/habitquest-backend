-- Kid login: a family login code (shared as a QR code) and a PIN for each child, so a child can sign in on
-- their own device. The code is made on request; families without one cannot be signed into this way.
ALTER TABLE hq.families ADD COLUMN login_code text UNIQUE;
ALTER TABLE hq.children ADD COLUMN kid_pin_hash text;

-- SECURITY DEFINER because the caller has no session yet: anyone holding the code sees the family's name and
-- each child's first name, buddy and whether a PIN is set. Nothing else.
CREATE FUNCTION hq.login_family(p_code text)
  RETURNS TABLE (family_id uuid, family_name text, child_id uuid, child_name text, buddy text, has_pin boolean)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  SELECT f.id, f.name, c.id, c.name, c.buddy, c.kid_pin_hash IS NOT NULL
  FROM hq.families f LEFT JOIN hq.children c ON c.family_id = f.id
  WHERE f.login_code = p_code
  ORDER BY c.created_at, c.id
$$;

-- The child's PIN hash (null when none is set), and no row unless the child belongs to the family with this code.
CREATE FUNCTION hq.login_child_pin(p_code text, p_child uuid) RETURNS TABLE (pin_hash text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  SELECT c.kid_pin_hash FROM hq.children c JOIN hq.families f ON f.id = c.family_id
  WHERE f.login_code = p_code AND c.id = p_child
$$;

REVOKE ALL ON FUNCTION hq.login_family(text), hq.login_child_pin(text, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION hq.login_family(text), hq.login_child_pin(text, uuid) TO hq_api;
