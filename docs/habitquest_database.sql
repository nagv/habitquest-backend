-- HabitQuest: the complete database in one script.
-- Generated from db/migrations/*.sql and db/seed.sql, which stay the source of truth.
--
-- Use it on a NEW, EMPTY database only, e.g.:
--   createdb habitquest
--   psql "postgresql://<user>@<host>:<port>/habitquest" -v ON_ERROR_STOP=1 -f habitquest_database.sql
-- The connecting user needs CREATEROLE (the script creates the hq_api and hq_kid roles).
--
-- It records each migration in public.hq_migrations with the same SHA-256 that scripts/setup_db.py uses, so
-- setup_db.py (and the API's start on Render) later sees them as applied and only runs newer ones.
-- It refuses to run on a database that already has migrations, because the first migration drops the hq schema.

BEGIN;
SELECT pg_advisory_xact_lock(28102026);
CREATE TABLE IF NOT EXISTS public.hq_migrations(name text PRIMARY KEY, digest text NOT NULL);
DO $guard$ BEGIN
  IF EXISTS (SELECT FROM public.hq_migrations) THEN
    RAISE EXCEPTION 'This database already has HabitQuest migrations. Use scripts/setup_db.py to update it.';
  END IF;
END $guard$;

-- ==== 202610310001_lean_schema.sql ----------------------------------------------------------------------------------

-- Lean MVP schema: families and caregivers, children, a flat catalog (goals -> habits), pain points mapped
-- to goals, the child's chosen habits, kid completions approved by a caregiver, and a points ledger.
-- No version columns; Sparks are counted from approved completions, points are summed from the ledger.

-- Replaces the earlier versioned onboarding schema. Hosted data so far is test data only.
DROP SCHEMA IF EXISTS hq CASCADE;
DROP SCHEMA IF EXISTS hq_checkpoint CASCADE;
DELETE FROM public.hq_migrations;

CREATE SCHEMA hq;
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'hq_api') THEN CREATE ROLE hq_api NOLOGIN; END IF;
END $$;
GRANT USAGE ON SCHEMA hq TO hq_api;

-- ── People ──────────────────────────────────────────────────────────────────────

-- A signed-in person. The id is the subject of our own access tokens; google_sub links a Google account.
CREATE TABLE hq.users(
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  google_sub text UNIQUE,
  email text,
  name text NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE hq.families(
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL CHECK (length(name) BETWEEN 1 AND 60),
  timezone text NOT NULL DEFAULT 'UTC',
  pin_hash text,
  invite_code text UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- One or more caregivers per family; a person may care for children in more than one family.
CREATE TABLE hq.caregivers(
  family_id uuid NOT NULL REFERENCES hq.families ON DELETE CASCADE,
  user_id uuid NOT NULL REFERENCES hq.users ON DELETE CASCADE,
  display_name text NOT NULL CHECK (length(display_name) BETWEEN 1 AND 40),
  role text NOT NULL CHECK (role IN ('father', 'mother', 'guardian', 'other')),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (family_id, user_id)
);
CREATE INDEX caregivers_user ON hq.caregivers(user_id);

-- routine_times: anchor -> "HH:MM", e.g. {"morning": "07:00", "bedtime": "20:30"}.
CREATE TABLE hq.children(
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  family_id uuid NOT NULL REFERENCES hq.families ON DELETE CASCADE,
  name text NOT NULL CHECK (length(name) BETWEEN 1 AND 40),
  age int NOT NULL CHECK (age BETWEEN 3 AND 13),
  buddy text NOT NULL DEFAULT 'fox' CHECK (buddy IN ('fox', 'owl', 'bear', 'bunny')),
  world text NOT NULL DEFAULT 'space' CHECK (world IN ('space', 'forest', 'city')),
  routine_times jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(routine_times) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX children_family ON hq.children(family_id);

-- ── Catalog: pre-approved goals and habits (is_active = approved and offered) ──

CREATE TABLE hq.goals(
  id text PRIMARY KEY CHECK (id ~ '^[a-z0-9-]+$'),
  title text NOT NULL,
  description text NOT NULL,
  keywords text[] NOT NULL DEFAULT '{}',
  is_active boolean NOT NULL DEFAULT true
);

CREATE TABLE hq.habits(
  id text PRIMARY KEY CHECK (id ~ '^[a-z0-9-]+$'),
  goal_id text NOT NULL REFERENCES hq.goals,
  title text NOT NULL,
  description text NOT NULL,
  kid_title text NOT NULL,
  kid_hint text NOT NULL,
  icon text NOT NULL,
  anchor text NOT NULL CHECK (anchor IN ('morning', 'midday', 'after_school', 'evening', 'bedtime')),
  min_age int NOT NULL,
  max_age int NOT NULL,
  tier int NOT NULL CHECK (tier BETWEEN 1 AND 3),
  points int NOT NULL CHECK (points > 0),
  is_active boolean NOT NULL DEFAULT true,
  CHECK (3 <= min_age AND min_age <= max_age AND max_age <= 13)
);
CREATE INDEX habits_goal ON hq.habits(goal_id) WHERE is_active;

-- ── Onboarding: pain points -> goals -> chosen habits ─────────────────────────

CREATE TABLE hq.pain_points(
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  child_id uuid NOT NULL REFERENCES hq.children ON DELETE CASCADE,
  text text NOT NULL CHECK (length(text) BETWEEN 5 AND 2000),
  mapped_by text NOT NULL CHECK (mapped_by IN ('ai', 'keywords', 'manual')),
  created_by uuid NOT NULL REFERENCES hq.users,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- A goal suggested for (or chosen for) a child, with the AI's reason and the habits it recommends.
CREATE TABLE hq.child_goals(
  child_id uuid NOT NULL REFERENCES hq.children ON DELETE CASCADE,
  goal_id text NOT NULL REFERENCES hq.goals,
  pain_point_id uuid REFERENCES hq.pain_points ON DELETE SET NULL,
  reason text,
  recommended_habit_ids text[] NOT NULL DEFAULT '{}',
  status text NOT NULL DEFAULT 'suggested' CHECK (status IN ('suggested', 'active', 'done', 'dropped')),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (child_id, goal_id)
);

-- The child's plan is simply their live rows here; ended rows keep the history.
CREATE TABLE hq.child_habits(
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  child_id uuid NOT NULL REFERENCES hq.children ON DELETE CASCADE,
  habit_id text NOT NULL REFERENCES hq.habits,
  cue_time time NOT NULL,
  days text[] NOT NULL CHECK (
    cardinality(days) BETWEEN 1 AND 7 AND days <@ ARRAY['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
  ),
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'removed')),
  started_on date NOT NULL DEFAULT current_date,
  ended_on date,
  CHECK (ended_on IS NULL OR ended_on >= started_on)
);
CREATE UNIQUE INDEX child_habits_live ON hq.child_habits(child_id, habit_id) WHERE status <> 'removed';

-- ── Daily loop ──────────────────────────────────────────────────────────────

-- One row per habit per day. The kid submits; a caregiver approves or rejects (the kid may resubmit).
CREATE TABLE hq.completions(
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  child_habit_id uuid NOT NULL REFERENCES hq.child_habits ON DELETE CASCADE,
  day date NOT NULL,
  status text NOT NULL DEFAULT 'submitted' CHECK (status IN ('submitted', 'approved', 'rejected')),
  submitted_at timestamptz NOT NULL DEFAULT now(),
  decided_by uuid REFERENCES hq.users,
  decided_at timestamptz,
  UNIQUE (child_habit_id, day),
  CHECK ((status = 'submitted') = (decided_at IS NULL) AND (decided_at IS NULL) = (decided_by IS NULL))
);

-- Points only (Sparks are the count of approved completions and are never spent).
-- earned: +points for one approved completion; redeemed: -points with a note of what it was used for.
CREATE TABLE hq.points_ledger(
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  child_id uuid NOT NULL REFERENCES hq.children ON DELETE CASCADE,
  amount int NOT NULL CHECK (amount <> 0),
  reason text NOT NULL CHECK (reason IN ('earned', 'redeemed', 'adjusted')),
  completion_id uuid UNIQUE REFERENCES hq.completions,
  note text CHECK (length(note) <= 200),
  created_by uuid REFERENCES hq.users,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (reason <> 'earned' OR (amount > 0 AND completion_id IS NOT NULL)),
  CHECK (reason <> 'redeemed' OR (amount < 0 AND completion_id IS NULL AND note IS NOT NULL))
);
CREATE INDEX points_ledger_child ON hq.points_ledger(child_id);

-- ── Row-level security: every family table is scoped to the caller's families ──

CREATE FUNCTION hq.my_user() RETURNS uuid LANGUAGE sql STABLE AS
  $$ SELECT NULLIF(current_setting('app.user_id', true), '')::uuid $$;

-- SECURITY DEFINER so the caregivers policy can use it without recursing into itself.
CREATE FUNCTION hq.my_families() RETURNS SETOF uuid LANGUAGE sql STABLE SECURITY DEFINER
  SET search_path = pg_catalog, pg_temp AS
  $$ SELECT family_id FROM hq.caregivers WHERE user_id = hq.my_user() $$;
REVOKE ALL ON FUNCTION hq.my_families() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION hq.my_families() TO hq_api;

ALTER TABLE hq.users ENABLE ROW LEVEL SECURITY;
CREATE POLICY own_user ON hq.users USING (id = hq.my_user());
ALTER TABLE hq.families ENABLE ROW LEVEL SECURITY;
CREATE POLICY my_family ON hq.families USING (id IN (SELECT hq.my_families()));
ALTER TABLE hq.caregivers ENABLE ROW LEVEL SECURITY;
CREATE POLICY my_family ON hq.caregivers USING (family_id IN (SELECT hq.my_families()));
ALTER TABLE hq.children ENABLE ROW LEVEL SECURITY;
CREATE POLICY my_family ON hq.children USING (family_id IN (SELECT hq.my_families()));
DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['pain_points', 'child_goals', 'child_habits', 'points_ledger'] LOOP
    EXECUTE format('ALTER TABLE hq.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY my_children ON hq.%I USING (child_id IN (SELECT id FROM hq.children))', t);
  END LOOP;
END $$;
ALTER TABLE hq.completions ENABLE ROW LEVEL SECURITY;
CREATE POLICY my_children ON hq.completions USING (child_habit_id IN (SELECT id FROM hq.child_habits));

GRANT SELECT ON hq.goals, hq.habits TO hq_api;
GRANT SELECT, INSERT, UPDATE, DELETE ON hq.users, hq.families, hq.caregivers, hq.children, hq.pain_points,
  hq.child_goals, hq.child_habits, hq.completions TO hq_api;
GRANT SELECT, INSERT ON hq.points_ledger TO hq_api;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA hq TO hq_api;

INSERT INTO public.hq_migrations VALUES ('202610310001_lean_schema.sql', '2e135b78cb72a1692b4119fc5f5c1b925c342e1b26e6b84c7efac8cf1b0ab6a8');

-- ==== 202610310002_accounts.sql -------------------------------------------------------------------------------------

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

INSERT INTO public.hq_migrations VALUES ('202610310002_accounts.sql', '802b2567680a96c6a6e8424d542ec1f8461277d34ff94b5c907af624a7e33566');

-- ==== 202610310003_daily_loop.sql -----------------------------------------------------------------------------------

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

INSERT INTO public.hq_migrations VALUES ('202610310003_daily_loop.sql', '1b9d9ee5bae1dac94722231e21b49a918bb35c2e71ff54dddd2451c18aa6c2ee');

-- ==== 202610310004_redemptions.sql ----------------------------------------------------------------------------------

-- Redemptions: a points balance can never go below zero.
-- A spending entry (negative amount) locks the child's row first, so two caregivers redeeming at once are
-- checked one after the other.

CREATE FUNCTION hq.points_never_negative() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.amount < 0 THEN
    PERFORM 1 FROM hq.children WHERE id = NEW.child_id FOR UPDATE;
    IF (SELECT coalesce(sum(amount), 0) FROM hq.points_ledger WHERE child_id = NEW.child_id) + NEW.amount < 0 THEN
      RAISE EXCEPTION 'not enough points' USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER points_never_negative BEFORE INSERT ON hq.points_ledger
  FOR EACH ROW EXECUTE FUNCTION hq.points_never_negative();

INSERT INTO public.hq_migrations VALUES ('202610310004_redemptions.sql', '634e2dde1a45326ea4784dc03af9e6ad955b2cce1b02e65e553f05e8f4674f9d');

-- ==== 202611010001_coach.sql ----------------------------------------------------------------------------------------

-- The habit coach: the conversation per child, the suggestions a caregiver approves, and a daily AI budget.

-- role: parent (typed), coach (the reply), note (an app status line, e.g. a suggestion was accepted).
-- shown_habits: habits the coach showed in this reply, so refining never repeats them.
CREATE TABLE hq.coach_messages(
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  child_id uuid NOT NULL REFERENCES hq.children ON DELETE CASCADE,
  role text NOT NULL CHECK (role IN ('parent', 'coach', 'note')),
  content text NOT NULL CHECK (length(content) BETWEEN 1 AND 6000),
  sources jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(sources) = 'array'),
  shown_habits text[] NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX coach_messages_child ON hq.coach_messages(child_id, id);

-- AI proposals waiting for a caregiver. Nothing applies until one is accepted with the family PIN.
CREATE TABLE hq.ai_suggestions(
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  child_id uuid NOT NULL REFERENCES hq.children ON DELETE CASCADE,
  source text NOT NULL CHECK (source IN ('coach', 'planner', 'reviewer')),
  kind text NOT NULL CHECK (kind IN ('add', 'retime', 'pause', 'resume', 'remove')),
  habit_id text NOT NULL REFERENCES hq.habits,
  cue_time time,
  reason text NOT NULL CHECK (length(reason) BETWEEN 1 AND 400),
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'dismissed')),
  created_at timestamptz NOT NULL DEFAULT now(),
  decided_by uuid REFERENCES hq.users,
  decided_at timestamptz,
  CHECK ((kind = 'retime') = (cue_time IS NOT NULL) OR kind = 'add'),
  CHECK ((status = 'pending') = (decided_at IS NULL))
);
CREATE UNIQUE INDEX ai_suggestions_one_pending ON hq.ai_suggestions(child_id, habit_id) WHERE status = 'pending';

-- Model-backed requests per family per day, so one family cannot run up the provider bill.
CREATE TABLE hq.ai_usage(
  family_id uuid NOT NULL REFERENCES hq.families ON DELETE CASCADE,
  day date NOT NULL,
  calls int NOT NULL DEFAULT 0,
  PRIMARY KEY (family_id, day)
);

DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['coach_messages', 'ai_suggestions'] LOOP
    EXECUTE format('ALTER TABLE hq.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY my_children ON hq.%I TO hq_api USING (child_id IN (SELECT id FROM hq.children))', t);
  END LOOP;
END $$;
ALTER TABLE hq.ai_usage ENABLE ROW LEVEL SECURITY;
CREATE POLICY my_family ON hq.ai_usage TO hq_api USING (family_id IN (SELECT hq.my_families()));

GRANT SELECT, INSERT, UPDATE, DELETE ON hq.coach_messages, hq.ai_suggestions, hq.ai_usage TO hq_api;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA hq TO hq_api;

INSERT INTO public.hq_migrations VALUES ('202611010001_coach.sql', '6a75b12240d145afab8adac5fe9f4d70e34c4f1ae19aab6616a0c8765a5f4149');

-- ==== 202611010002_reports.sql --------------------------------------------------------------------------------------

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

INSERT INTO public.hq_migrations VALUES ('202611010002_reports.sql', 'd84cc0b619a699fc6f07015307ffdd73c7999534c935673a2fb906e17ca51f59');

-- ==== 202611010003_family_features.sql ------------------------------------------------------------------------------

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

INSERT INTO public.hq_migrations VALUES ('202611010003_family_features.sql', 'e016044b2d23b5833ee8ccbdf99341d385ec59b083dd4b5627c1bf684a91eefe');

-- ==== 202611020001_world_journey.sql --------------------------------------------------------------------------------

-- The world journey: Sparks now equal a quest's tier, capped per day, and walk a child through six worlds.

-- ── Six worlds ──────────────────────────────────────────────────────────────
ALTER TABLE hq.children DROP CONSTRAINT children_world_check;
ALTER TABLE hq.children ADD CONSTRAINT children_world_check
  CHECK (world IN ('camp', 'mosswood', 'sky', 'coral', 'crystal', 'star'));

-- A parent can open harder quests early or hold them back. 0 follows the child's world.
ALTER TABLE hq.children ADD COLUMN tier_override int NOT NULL DEFAULT 0 CHECK (tier_override BETWEEN 0 AND 3);

-- ── Nobody moves backwards ──────────────────────────────────────────────────
-- Worlds now cost more Sparks. Children who already reached a world keep it through journey_bonus, which is added to
-- the Sparks they earn, as are the quest tiers they had opened (old: 5 and 15 approved quests; new: 30 and 90 Sparks).
ALTER TABLE hq.child_extras ADD COLUMN journey_bonus int NOT NULL DEFAULT 0 CHECK (journey_bonus >= 0);

WITH days AS (
  SELECT k.child_id, c.day, count(*) AS quests, least(6, sum(h.tier)) AS sparks
  FROM hq.completions c
  JOIN hq.child_habits k ON k.id = c.child_habit_id
  JOIN hq.habits h ON h.id = k.habit_id
  WHERE c.status = 'approved'
  GROUP BY k.child_id, c.day
), totals AS (
  SELECT child_id, sum(quests) AS old_sparks, sum(sparks) AS new_sparks FROM days GROUP BY child_id
), kept AS (
  SELECT child_id, new_sparks, CASE WHEN old_sparks >= 15 THEN 90 WHEN old_sparks >= 5 THEN 30 ELSE 0 END AS floor
  FROM totals
)
UPDATE hq.child_extras x
SET journey_bonus = greatest(0, kept.floor - kept.new_sparks),
    level_seen = CASE WHEN kept.floor = 90 THEN 3 WHEN kept.floor = 30 THEN 2 ELSE 1 END
FROM kept WHERE kept.child_id = x.child_id;

UPDATE hq.children k
SET world = CASE x.level_seen WHEN 3 THEN 'sky' WHEN 2 THEN 'mosswood' ELSE 'camp' END
FROM hq.child_extras x WHERE x.child_id = k.id;

-- ── A child can change their own buddy ──────────────────────────────────────
-- Kids cannot write hq.children. This function changes only their own buddy.
CREATE FUNCTION hq.kid_set_buddy(p_buddy text) RETURNS void LANGUAGE plpgsql
  SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  UPDATE hq.children SET buddy = p_buddy WHERE id = hq.my_kid();
END $$;
REVOKE ALL ON FUNCTION hq.kid_set_buddy(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION hq.kid_set_buddy(text) TO hq_kid;

-- ── Suggestions that come from a child mastering a quest ────────────────────
ALTER TABLE hq.ai_suggestions DROP CONSTRAINT ai_suggestions_source_check;
ALTER TABLE hq.ai_suggestions ADD CONSTRAINT ai_suggestions_source_check
  CHECK (source IN ('coach', 'planner', 'reviewer', 'mastery'));

INSERT INTO public.hq_migrations VALUES ('202611020001_world_journey.sql', '4cccbebfa77dccd08101201366be34af19b19086a42318eb1db6f0fae26aa966');

-- ==== 202611020002_coach_no_habit_match.sql -------------------------------------------------------------------------

-- A coach reply that looked for habits and found none suitable, so the app can say so beside the tips.
ALTER TABLE hq.coach_messages ADD COLUMN no_habit_match boolean NOT NULL DEFAULT false;

INSERT INTO public.hq_migrations VALUES ('202611020002_coach_no_habit_match.sql', 'fb704712e8518905a8e68f5cf6279559b5bee3648b0fbd5d785a49ae4b6b4f94');

-- ==== 202611030001_kid_login.sql ------------------------------------------------------------------------------------

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

INSERT INTO public.hq_migrations VALUES ('202611030001_kid_login.sql', 'f5ee4d1122c1347d80d18de5bac81ae43dab619a8af419390d5d072edf31ca94');

GRANT hq_api, hq_kid TO CURRENT_USER;

-- ==== seed.sql ------------------------------------------------------------------------------------------------------

-- The pre-approved catalog, applied on every start (scripts/setup_db.py). Rows are upserted by id, so editing
-- a row here and redeploying updates it. To stop offering a goal or habit, set is_active = false (rows are
-- never deleted: children's plans and history refer to them).
-- Habits come from the frontend's quest library; tier 1 pays 10 points, tier 2 pays 15, tier 3 pays 20.

INSERT INTO hq.goals(id, title, description, keywords, is_active) VALUES
  ('bedtime', 'Calmer bedtimes', 'Getting to bed on time without stalling, so everyone sleeps better.',
   '{bed,bedtime,sleep,night,lights out,stall,tired,teeth}', true),
  ('mornings', 'Smoother mornings', 'Up, dressed and ready on time without a rush or repeated reminders.',
   '{morning,wake,late,rushed,dressed,school run,ready}', true),
  ('meals', 'Happier mealtimes', 'Calm family meals: eating on time, trying new food and helping at the table.',
   '{meal,dinner,lunch,eat,food,picky,vegetable,table,snack}', true),
  ('homework', 'Homework without battles', 'Starting and finishing schoolwork and reading with less arguing.',
   '{homework,study,school,reading,read,focus,practice,learning}', true),
  ('screens', 'Healthy screen time', 'Screens within limits and switched off without a fight, with more active play.',
   '{screen,tablet,ipad,tv,phone,games,gaming,youtube,device}', true),
  ('responsibility', 'Helping at home', 'Tidying up and doing small chores without being asked again and again.',
   '{messy,tidy,clean,chores,room,toys,clothes,help,laundry}', true),
  ('feelings', 'Handling big feelings', 'Naming feelings, calming down and being kind when emotions run high.',
   '{tantrum,meltdown,angry,upset,cry,rude,feelings,worry,anxious,kind,manners}', true),
  ('listening', 'Listening the first time', 'Hearing an instruction, answering it and doing it without repeated asking.',
   '{listen,ignore,instruction,obey,respond,not hearing,selective hearing,repeat myself,asking again,does not answer}',
   true)
ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, description = EXCLUDED.description,
  keywords = EXCLUDED.keywords, is_active = EXCLUDED.is_active;

INSERT INTO hq.habits(id, goal_id, title, description, kid_title, kid_hint, icon, anchor, min_age, max_age, tier,
                      points, is_active) VALUES
  -- bedtime
  ('bedtime', 'bedtime', 'Get to bed early', 'Teeth brushed and in bed by bedtime, for a good night''s sleep.',
   'Sleepy time', 'Snuggle into bed on time', '🌙', 'bedtime', 3, 13, 1, 10, true),
  ('brush-teeth', 'bedtime', 'Brush teeth morning and night', 'Two minutes, twice a day, without a battle.',
   'Sparkly teeth', 'Brush in the morning and at night', '🪥', 'bedtime', 3, 13, 1, 10, true),
  ('story-time', 'bedtime', 'Share a book every day', 'Reading together or alone, every day, even for a few minutes.',
   'Story time', 'Read a book or listen to a story', '📖', 'bedtime', 3, 13, 1, 10, true),
  ('screen-sunset', 'bedtime', 'No screens an hour before bed',
   'Screens off an hour before bedtime helps children fall asleep.',
   'Screen sunset', 'Screens off one hour before bed', '🌇', 'evening', 5, 13, 2, 15, true),
  -- mornings
  ('wake', 'mornings', 'Wake up early', 'A calm, unhurried start: up and out of bed by the time you choose.',
   'Rise and shine!', 'Hop out of bed when the sun says hello', '🌅', 'morning', 3, 13, 1, 10, true),
  ('dress-self', 'mornings', 'Get dressed independently',
   'Getting dressed without help, to build independence in the morning.',
   'Dress-up champ', 'Put on your clothes all by yourself', '👕', 'morning', 3, 8, 1, 10, true),
  ('make-bed', 'mornings', 'Make the bed', 'Pull up the covers and straighten the pillow every morning.',
   'Bed builder', 'Pull up the covers and fluff your pillow', '🛏️', 'morning', 5, 13, 2, 15, true),
  ('pack-bag', 'mornings', 'Pack the school bag the night before',
   'A calmer morning starts with the bag packed the evening before.',
   'Bag ready', 'Pack your bag for tomorrow', '🎒', 'evening', 5, 13, 2, 15, true),
  ('plan-tomorrow', 'mornings', 'Plan for tomorrow',
   'Clothes picked and tomorrow''s plan checked the evening before.',
   'Tomorrow planner', 'Pick your clothes and plan tomorrow', '🗓️', 'evening', 8, 13, 3, 20, true),
  -- meals
  ('lunch', 'meals', 'Finish lunch on time', 'Sit down and finish lunch without a long chase around the table.',
   'Yummy lunch', 'Finish your lunch plate', '🍱', 'midday', 3, 13, 1, 10, true),
  ('dinner', 'meals', 'Finish dinner on time', 'Family dinner finished on time, so the evening routine stays relaxed.',
   'Dinner done', 'Finish your dinner plate', '🍲', 'evening', 3, 13, 1, 10, true),
  ('wash-hands', 'meals', 'Wash hands before meals', 'Soap and water before every meal and after the bathroom.',
   'Bubble hands', 'Wash with soap before you eat', '🧼', 'midday', 3, 10, 1, 10, true),
  ('water', 'meals', 'Drink water with meals', 'A glass of water with each meal instead of sweet drinks.',
   'Water power', 'Drink water with every meal', '💧', 'midday', 3, 13, 1, 10, true),
  ('veggie', 'meals', 'Try a vegetable', 'One brave bite of a vegetable each day, no pressure to finish.',
   'Veggie explorer', 'Take one brave bite of a veggie', '🥦', 'evening', 3, 13, 1, 10, true),
  ('set-table', 'meals', 'Help set the table', 'Helping with family meals builds teamwork.',
   'Table helper', 'Help set the table for a meal', '🍽️', 'evening', 4, 13, 2, 15, true),
  ('family-table', 'meals', 'Device-free family meal', 'One meal a day together with no phones, tablets or TV.',
   'Family table', 'Eat together with no screens', '🥗', 'evening', 3, 13, 2, 15, true),
  -- homework
  ('homework', 'homework', 'Complete homework daily',
   'Homework finished every school day, before play time takes over.',
   'Homework hero', 'Finish today''s homework', '📒', 'after_school', 3, 13, 1, 10, true),
  ('read-20', 'homework', 'Read for 20 minutes', 'Twenty minutes of reading builds a lasting reading habit.',
   'Reading quest', 'Read for 20 minutes', '📚', 'after_school', 6, 13, 2, 15, true),
  ('homework-start', 'homework', 'Start homework without reminders',
   'Getting started on homework without being asked.',
   'Self-starter', 'Start your homework all by yourself', '📝', 'after_school', 7, 13, 3, 20, true),
  ('practice', 'homework', 'Practise a skill for 15 minutes',
   'Music, sport, art or anything they''re learning, a little every day.',
   'Skill builder', 'Practise music, sport or art for 15 minutes', '🎹', 'after_school', 6, 13, 3, 20, true),
  -- screens
  ('screen', 'screens', 'Reduce screen time',
   'Stay within a daily screen-time limit across TV, tablet and games.',
   'Screen-time saver', 'Keep screens short and play more', '📱', 'after_school', 3, 13, 1, 10, true),
  ('outdoor-play', 'screens', 'Play outside', 'Time outdoors running, climbing or exploring.',
   'Outside adventure', 'Run, jump and play outside', '⚽', 'after_school', 3, 13, 1, 10, true),
  ('screen-switch-off', 'screens', 'Switch off screens when time is up',
   'Turning the screen off when the timer rings, without a fuss.',
   'Screen switch-off', 'Turn off the screen when the timer rings', '⏰', 'after_school', 4, 13, 2, 15, true),
  ('move-30', 'screens', 'Active play for 30 minutes', 'Half an hour of dancing, biking or running games.',
   'Move and groove', 'Dance, bike or play for 30 minutes', '🤸', 'after_school', 4, 13, 2, 15, true),
  -- responsibility
  ('toys-away', 'responsibility', 'Tidy toys after play', 'Toys go back where they live when play is over.',
   'Toy tidy-up', 'Put your toys back home', '🧸', 'evening', 3, 10, 1, 10, true),
  ('tidy-space', 'responsibility', 'Five-minute tidy', 'Five minutes to put their own space back in order.',
   'Tidy sprint', 'Tidy your room for 5 minutes', '🧹', 'evening', 8, 13, 1, 10, true),
  ('laundry', 'responsibility', 'Clothes in the laundry basket', 'Dirty clothes go in the basket, not on the floor.',
   'Laundry hoop', 'Toss your clothes in the basket', '🧺', 'evening', 4, 13, 2, 15, true),
  ('caretaker', 'responsibility', 'Care for a pet or plant', 'Feeding a pet or watering a plant, every day.',
   'Caretaker', 'Feed a pet or water a plant', '🐾', 'after_school', 5, 13, 3, 20, true),
  ('chore-champion', 'responsibility', 'Finish a chore independently',
   'One household chore done start to finish without help.',
   'Chore champion', 'Finish one chore without help', '🧽', 'after_school', 6, 13, 3, 20, true),
  -- feelings
  ('name-feeling', 'feelings', 'Name a feeling', 'Putting feelings into words helps children handle big emotions.',
   'Feelings check', 'Tell someone how you feel today', '😊', 'evening', 3, 10, 1, 10, true),
  ('magic-words', 'feelings', 'Say please and thank you', 'Using polite words with family, friends and teachers.',
   'Magic words', 'Say please and thank you', '🙏', 'morning', 3, 10, 1, 10, true),
  ('day-chat', 'feelings', 'Talk about the day', 'A few minutes sharing how the day went, the good and the hard parts.',
   'Day chat', 'Tell a grown-up about your day', '💬', 'evening', 8, 13, 1, 10, true),
  ('calm-breaths', 'feelings', 'Calm-down breathing', 'Five slow breaths when upset, a simple tool for big feelings.',
   'Dragon breaths', 'Take 5 slow dragon breaths when you feel upset', '🌬️', 'after_school', 4, 13, 2, 15, true),
  ('kind-act', 'feelings', 'One kind act a day', 'Helping, sharing or including someone, once a day.',
   'Kindness mission', 'Help someone or share something', '💛', 'after_school', 4, 13, 2, 15, true),
  ('happy-three', 'feelings', 'Share three good things',
   'Naming three good things about the day builds a positive outlook.',
   'Happy three', 'Tell three good things about today', '🌟', 'bedtime', 5, 13, 3, 20, true),
  ('day-diary', 'feelings', 'Draw or write about the day',
   'A few lines or a drawing about the day helps with reflection.',
   'Day diary', 'Draw or write about today', '✏️', 'bedtime', 6, 13, 3, 20, true),
  -- listening
  ('answer-call', 'listening', 'Answer when called', 'Stopping, looking up and answering the first time a grown-up calls.',
   'Super ears', 'Look up and say "okay" when a grown-up calls you', '👂', 'after_school', 3, 13, 1, 10, true),
  ('first-ask', 'listening', 'Do one ask the first time', 'Doing one instruction after being asked once, with no reminder.',
   'First-time listener', 'Do one thing the first time you are asked', '✅', 'after_school', 3, 13, 1, 10, true),
  ('say-it-back', 'listening', 'Say the instruction back', 'Repeating an instruction in their own words, to show it was heard.',
   'Echo power', 'Say back what the grown-up asked you to do', '🦜', 'evening', 4, 13, 1, 10, true),
  ('not-fun-first', 'listening', 'Do the not-fun ask right away',
   'Doing one uninteresting job when asked, before going back to play.',
   'Boring-job beater', 'Do a not-fun job when asked, then go back to play', '💪', 'after_school', 5, 13, 2, 15, true),
  ('two-step-ask', 'listening', 'Finish a two-step ask', 'Remembering and finishing two steps with no reminder.',
   'Two-step star', 'Finish both steps of an ask without a reminder', '👣', 'evening', 6, 13, 3, 20, true)
ON CONFLICT (id) DO UPDATE SET goal_id = EXCLUDED.goal_id, title = EXCLUDED.title,
  description = EXCLUDED.description, kid_title = EXCLUDED.kid_title, kid_hint = EXCLUDED.kid_hint,
  icon = EXCLUDED.icon, anchor = EXCLUDED.anchor, min_age = EXCLUDED.min_age, max_age = EXCLUDED.max_age,
  tier = EXCLUDED.tier, points = EXCLUDED.points, is_active = EXCLUDED.is_active;

COMMIT;
