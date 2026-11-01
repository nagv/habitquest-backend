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
