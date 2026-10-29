-- plan_flags and catalog_requests for plan 2b (change sets, PATCH edits, feedback).

CREATE TABLE hq.plan_flags(
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  plan_id uuid NOT NULL REFERENCES hq.plans,
  kind text NOT NULL CHECK(kind IN ('habit_withdrawn','cue_removed','level_removed')),
  habit_id text NOT NULL,
  reason text NOT NULL CHECK(char_length(reason) <= 300),
  created_at timestamptz NOT NULL DEFAULT now(),
  resolved_at timestamptz
);
ALTER TABLE hq.plan_flags ENABLE ROW LEVEL SECURITY;
CREATE POLICY parent_scope ON hq.plan_flags
  USING(plan_id IN (SELECT id FROM hq.plans WHERE parent_id = hq.my_parent()));
-- The API flags plans when a profile edit removes a routine cue.
GRANT SELECT, INSERT ON hq.plan_flags TO hq_api;
GRANT INSERT, UPDATE ON hq.plan_flags TO hq_worker;

-- Anonymised unmatched concern phrases; no child or household identifiers.
CREATE TABLE hq.catalog_requests(
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  phrase_hash text NOT NULL UNIQUE CHECK(char_length(phrase_hash) = 32),
  normalized_phrase text NOT NULL CHECK(char_length(normalized_phrase) BETWEEN 1 AND 200),
  count int NOT NULL DEFAULT 1 CHECK(count > 0),
  first_seen timestamptz NOT NULL DEFAULT now(),
  last_seen timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT, INSERT, UPDATE ON hq.catalog_requests TO hq_worker;
