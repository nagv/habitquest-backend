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
