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
