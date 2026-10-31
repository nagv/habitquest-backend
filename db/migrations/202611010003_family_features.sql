-- What used to live in the browser: the reward shop, check-ins, surprises, kid extras and family settings.

-- ── Points ──────────────────────────────────────────────────────────────────
-- bonus: chests and surprises; spent: a kid's request; refund: a declined request.
ALTER TABLE hq.points_ledger DROP CONSTRAINT points_ledger_reason_check;
ALTER TABLE hq.points_ledger ADD CONSTRAINT points_ledger_reason_check
  CHECK (reason IN ('earned', 'redeemed', 'adjusted', 'bonus', 'spent', 'refund'));
ALTER TABLE hq.points_ledger ADD CHECK (reason NOT IN ('bonus', 'refund') OR (amount > 0 AND note IS NOT NULL));
ALTER TABLE hq.points_ledger ADD CHECK (reason <> 'spent' OR (amount < 0 AND note IS NOT NULL));

-- Kids cannot write the ledger directly. This function lets the API record what a kid token earned or spent,
-- for that child only and within fixed limits. The points trigger still stops a negative balance.
CREATE FUNCTION hq.kid_ledger(p_amount int, p_reason text, p_note text) RETURNS void LANGUAGE plpgsql
  SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  IF NOT ((p_reason = 'bonus' AND p_amount BETWEEN 1 AND 100) OR (p_reason = 'spent' AND p_amount < 0)) THEN
    RAISE EXCEPTION 'kid ledger entry not allowed' USING ERRCODE = 'check_violation';
  END IF;
  INSERT INTO hq.points_ledger(child_id, amount, reason, note) VALUES (hq.my_kid(), p_amount, p_reason, p_note);
END $$;
REVOKE ALL ON FUNCTION hq.kid_ledger(int, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION hq.kid_ledger(int, text, text) TO hq_kid;

-- ── Worlds now match the frontend's unlockable worlds ───────────────────────
ALTER TABLE hq.children DROP CONSTRAINT children_world_check;
UPDATE hq.children SET world = CASE world WHEN 'forest' THEN 'mosswood' WHEN 'city' THEN 'sky' ELSE 'camp' END;
ALTER TABLE hq.children ALTER COLUMN world SET DEFAULT 'camp';
ALTER TABLE hq.children ADD CONSTRAINT children_world_check CHECK (world IN ('camp', 'mosswood', 'sky'));

-- ── The reward shop ─────────────────────────────────────────────────────────
CREATE TABLE hq.rewards(
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  family_id uuid NOT NULL REFERENCES hq.families ON DELETE CASCADE,
  title text NOT NULL CHECK (length(title) BETWEEN 1 AND 80),
  emoji text NOT NULL CHECK (length(emoji) BETWEEN 1 AND 8),
  cost int NOT NULL CHECK (cost BETWEEN 1 AND 100000),
  kind text NOT NULL CHECK (kind IN ('screen', 'privilege', 'outing', 'toy', 'mystery')),
  minutes int CHECK (minutes BETWEEN 1 AND 240),
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX rewards_family ON hq.rewards(family_id);

CREATE TABLE hq.reward_requests(
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  child_id uuid NOT NULL REFERENCES hq.children ON DELETE CASCADE,
  reward_id uuid REFERENCES hq.rewards ON DELETE SET NULL,
  title text NOT NULL,
  emoji text NOT NULL,
  cost int NOT NULL CHECK (cost > 0),
  minutes int,
  mystery boolean NOT NULL DEFAULT false,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'declined')),
  day date NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  decided_by uuid REFERENCES hq.users,
  decided_at timestamptz
);
CREATE INDEX reward_requests_child ON hq.reward_requests(child_id, status);

-- ── Check-ins: one per child per day; a newer one replaces it ───────────────
CREATE TABLE hq.checkins(
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  child_id uuid NOT NULL REFERENCES hq.children ON DELETE CASCADE,
  day date NOT NULL,
  mood text NOT NULL CHECK (mood IN ('great', 'good', 'okay', 'hard')),
  hardest text REFERENCES hq.habits,
  favorite text REFERENCES hq.habits,
  stickers text[] NOT NULL DEFAULT '{}',
  note text CHECK (length(note) <= 200),
  flagged text,
  read_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (child_id, day)
);

-- ── Surprises: child_id NULL means every child in the family ────────────────
CREATE TABLE hq.surprises(
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  family_id uuid NOT NULL REFERENCES hq.families ON DELETE CASCADE,
  child_id uuid REFERENCES hq.children ON DELETE CASCADE,
  kind text NOT NULL CHECK (kind IN ('note', 'double')),
  message text NOT NULL DEFAULT '' CHECK (length(message) <= 140),
  points int NOT NULL DEFAULT 0 CHECK (points BETWEEN 0 AND 100 AND (kind = 'note' OR points = 0)),
  day date NOT NULL,
  created_by uuid REFERENCES hq.users,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX surprises_family ON hq.surprises(family_id, day);

CREATE TABLE hq.surprise_reveals(
  surprise_id uuid NOT NULL REFERENCES hq.surprises ON DELETE CASCADE,
  child_id uuid NOT NULL REFERENCES hq.children ON DELETE CASCADE,
  revealed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (surprise_id, child_id)
);

-- ── Kid extras and family settings: one row each, created with the parent row ──
CREATE TABLE hq.child_extras(
  child_id uuid PRIMARY KEY REFERENCES hq.children ON DELETE CASCADE,
  discoveries text[] NOT NULL DEFAULT '{}',
  accessories text[] NOT NULL DEFAULT '{}',
  wearing text,
  goal uuid REFERENCES hq.rewards ON DELETE SET NULL,
  chests date[] NOT NULL DEFAULT '{}',
  level_seen int NOT NULL DEFAULT 1,
  app_seconds jsonb NOT NULL DEFAULT '{}',
  app_extra_minutes jsonb NOT NULL DEFAULT '{}',
  screen_bonus jsonb NOT NULL DEFAULT '{}'
);

CREATE TABLE hq.family_settings(
  family_id uuid PRIMARY KEY REFERENCES hq.families ON DELETE CASCADE,
  app_limit int NOT NULL DEFAULT 0 CHECK (app_limit BETWEEN 0 AND 240),
  buddy_talk boolean NOT NULL DEFAULT true
);

CREATE FUNCTION hq.default_rewards(p_family uuid) RETURNS void LANGUAGE sql AS $$
  INSERT INTO hq.rewards(family_id, title, emoji, cost, kind, minutes) VALUES
    (p_family, 'Mystery box', '🎁', 40, 'mystery', NULL),
    (p_family, '+15 minutes screen time', '📺', 60, 'screen', 15),
    (p_family, 'Pick what''s for dinner', '🍕', 80, 'privilege', NULL),
    (p_family, '+30 minutes screen time', '🎮', 110, 'screen', 30),
    (p_family, 'Stay up 15 minutes later (weekend)', '🌙', 120, 'privilege', NULL),
    (p_family, 'Choose the family movie', '🍿', 150, 'outing', NULL),
    (p_family, 'Special trip to the park', '🛝', 200, 'outing', NULL),
    (p_family, 'A new small toy', '🧸', 400, 'toy', NULL)
$$;

-- SECURITY DEFINER because the new family's first caregiver is added only after this row.
CREATE FUNCTION hq.family_defaults() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  INSERT INTO hq.family_settings(family_id) VALUES (NEW.id);
  PERFORM hq.default_rewards(NEW.id);
  RETURN NEW;
END $$;
CREATE TRIGGER family_defaults AFTER INSERT ON hq.families FOR EACH ROW EXECUTE FUNCTION hq.family_defaults();

CREATE FUNCTION hq.child_defaults() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  INSERT INTO hq.child_extras(child_id) VALUES (NEW.id);
  RETURN NEW;
END $$;
CREATE TRIGGER child_defaults AFTER INSERT ON hq.children FOR EACH ROW EXECUTE FUNCTION hq.child_defaults();

INSERT INTO hq.family_settings(family_id) SELECT id FROM hq.families;
SELECT hq.default_rewards(id) FROM hq.families;
INSERT INTO hq.child_extras(child_id) SELECT id FROM hq.children;

-- ── Row-level security ──────────────────────────────────────────────────────
DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['rewards', 'surprises', 'family_settings'] LOOP
    EXECUTE format('ALTER TABLE hq.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY my_family ON hq.%I TO hq_api USING (family_id IN (SELECT hq.my_families()))', t);
  END LOOP;
  FOREACH t IN ARRAY ARRAY['reward_requests', 'checkins', 'surprise_reveals', 'child_extras'] LOOP
    EXECUTE format('ALTER TABLE hq.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY my_children ON hq.%I TO hq_api USING (child_id IN (SELECT id FROM hq.children))', t);
    EXECUTE format('CREATE POLICY kid_self ON hq.%I TO hq_kid USING (child_id = hq.my_kid()) WITH CHECK (child_id = hq.my_kid())', t);
  END LOOP;
END $$;
-- Under hq_kid, hq.children shows only the child's own row, so this is the child's own family.
CREATE POLICY kid_family ON hq.rewards FOR SELECT TO hq_kid USING (family_id IN (SELECT family_id FROM hq.children));
CREATE POLICY kid_family ON hq.family_settings FOR SELECT TO hq_kid
  USING (family_id IN (SELECT family_id FROM hq.children));
CREATE POLICY kid_family ON hq.surprises FOR SELECT TO hq_kid
  USING (family_id IN (SELECT family_id FROM hq.children) AND (child_id IS NULL OR child_id = hq.my_kid()));

GRANT SELECT, INSERT, UPDATE ON hq.rewards, hq.reward_requests, hq.checkins, hq.surprises, hq.child_extras,
  hq.family_settings TO hq_api;
GRANT SELECT ON hq.surprise_reveals TO hq_api;
GRANT SELECT ON hq.rewards, hq.family_settings, hq.surprises TO hq_kid;
GRANT SELECT, INSERT ON hq.reward_requests, hq.surprise_reveals TO hq_kid;
GRANT SELECT, INSERT, UPDATE ON hq.checkins TO hq_kid;
GRANT SELECT, UPDATE ON hq.child_extras TO hq_kid;
