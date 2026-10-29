# Catalog Foundation Implementation Plan (Plan 1 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the versioned, two-person-reviewed habit catalog with numbered releases, seed release 1 from today's catalog, and provide tested Python eligibility, scoring and template-plan functions that later plans wire into planning.

**Architecture:** A new forward-only SQL migration adds catalog tables, immutability triggers and read-only "released_*" views; `hq_api` and `hq_worker` may read only those views. A new `habitquest_backend.catalog` package loads a release into frozen dataclasses and runs pure, deterministic eligibility and scoring over it. The existing `hq.catalog`/`hq.chapters` tables and the current planner stay untouched, so every existing test keeps passing; Plan 2 switches planning to this package and drops the old tables.

**Tech Stack:** Python 3.12, uv, psycopg 3, PostgreSQL (native or Supabase local), pytest, Hypothesis, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-10-29-habit-catalog-design.md` (sections 3.1, 3.3, 4.2 steps 2-3, 6 step 2, 8).

---

## Roadmap

| Plan | Scope | Depends on |
|---|---|---|
| **1 (this)** | Catalog schema, release 1 seed, read views, `catalog` package (repository, eligibility, scoring, template plan) | — |
| 2 | Child profile columns and vocabularies, `intakes`, `child_goals`, `plan_flags`, single planning job (LLM map → eligibility → score → LLM pick → validate → review → template fallback), proposal `PATCH`, change-set confirmation, migration of sessions/preferences, removal of `hq.catalog`, `hq.chapters`, `Category`, analysis jobs; move fake model to test support; concern dataset | Plan 1 |
| 3 | `Editor` auth dependency, `hq_editor` role, lifecycle functions with audit, release build/diff/validate/publish, withdrawal with `plan_flags`, `catalog_requests`, editor endpoints | Plans 1 and 2 |

Plans 2 and 3 are written after this plan merges, against the interfaces it creates.

## Design refinements recorded by this plan

These clarify the spec; Task 6 updates the spec text to match.

1. Eligibility runs in Python over a loaded release. The SQL guarantee is that service roles can only read published, approved rows through `released_*` views.
2. No `tsvector` column on concerns: the LLM performs concern mapping (approach A), so full-text search is not needed yet.
3. `catalog_releases.id` is the release number; there is no separate `number` column.
4. Interest variants belong to a habit version and are reviewed with it; they have no separate status.
5. Accessibility: each of the child's needs `x` must appear on the habit as tag `access:x`.
6. Unresolved goal reasons are `no_catalog_entry`, `needs_cue` and `deferred` (blocked by `max_active_habits` or the minutes budget).
7. Release 1 content is seeded by `supabase/seed.sql` (idempotent), matching how the existing development catalog is seeded and reloaded by test fixtures.

## File structure

| File | Responsibility |
|---|---|
| Create `supabase/migrations/202610290001_catalog.sql` | Catalog tables, constraints, immutability triggers, released views, grants |
| Modify `supabase/seed.sql` | Append idempotent release 1 seed |
| Modify `tests/conftest.py:61` | Truncate new catalog tables before reseeding |
| Create `src/habitquest_backend/catalog/__init__.py` | Package marker |
| Create `src/habitquest_backend/catalog/models.py` | Frozen dataclasses: `Level`, `Habit`, `Goal`, `Concern`, `Chapter`, `Release`, `RoutineSlot`, `ChildProfile` |
| Create `src/habitquest_backend/catalog/repository.py` | `load_release(conn, release_id=None)` |
| Create `src/habitquest_backend/catalog/eligibility.py` | `Candidate`, `eligible(...)` |
| Create `src/habitquest_backend/catalog/scoring.py` | `Scored`, `TemplatePlan`, `score`, `shortlists`, `template_plan`, `RANKING_VERSION` |
| Create `tests/catalog_factories.py` | Builders for in-memory habits and releases |
| Create `tests/test_catalog_schema.py` | Constraint, trigger, role and seed tests against the real database |
| Create `tests/test_catalog_repository.py` | `load_release` against release 1 |
| Create `tests/test_catalog_eligibility.py` | Pure eligibility tests |
| Create `tests/test_catalog_scoring.py` | Pure scoring, shortlist, template plan and Hypothesis property tests |

## Before you start

The test database must exist and be migrated. From the repository root in PowerShell:

```powershell
uv sync --locked
$env:DATABASE_URL = (Select-String -Path .env -Pattern '^TEST_DATABASE_URL=(.*)$').Matches[0].Groups[1].Value
uv run python scripts/setup_db.py
uv run python -m pytest -q
```

Expected: `79 passed` and `Required test coverage of 100% reached`. If `.env` has no `TEST_DATABASE_URL`, run `uv run python scripts/bootstrap_local.py --native` first. After every new migration in this plan, rerun the `setup_db.py` line above (it applies only unapplied migrations).

Work on branch `design/habit-catalog` (already checked out) or a worktree from it.

---

### Task 1: Catalog schema, guards and views

**Files:**
- Create: `supabase/migrations/202610290001_catalog.sql`
- Modify: `tests/conftest.py:61`
- Test: `tests/test_catalog_schema.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_catalog_schema.py`:

```python
import psycopg
import pytest

AUTHOR = "00000000-0000-0000-0000-0000000000a1"
REVIEWER = "00000000-0000-0000-0000-0000000000a2"


def draft_habit(db, habit_id="test-habit"):
    db.execute("INSERT INTO hq.habits VALUES(%s)", (habit_id,))
    db.execute(
        "INSERT INTO hq.habit_versions(habit_id,version,title,instruction,min_age,max_age,routine_anchor,created_by) "
        "VALUES(%s,1,'Test habit','Do the agreed thing.',6,13,'bedtime',%s)",
        (habit_id, AUTHOR),
    )
    db.execute(
        "INSERT INTO hq.habit_levels(habit_id,version,level,minutes,points) VALUES(%s,1,'standard',5,1)",
        (habit_id,),
    )


def approve_habit(db, habit_id="test-habit"):
    db.execute(
        "UPDATE hq.habit_versions SET status='approved',reviewed_by=%s,reviewed_at=now() WHERE habit_id=%s",
        (REVIEWER, habit_id),
    )


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE hq.habit_versions SET title='Changed' WHERE habit_id='test-habit'",
        "INSERT INTO hq.habit_levels(habit_id,version,level,minutes,points) VALUES('test-habit',1,'simple',2,1)",
        "UPDATE hq.habit_levels SET minutes=9 WHERE habit_id='test-habit'",
        "DELETE FROM hq.habit_levels WHERE habit_id='test-habit'",
        "DELETE FROM hq.habit_versions WHERE habit_id='test-habit'",
    ],
)
def test_approved_habit_version_and_details_are_immutable(db, statement):
    draft_habit(db)
    approve_habit(db)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute(statement)


def test_approved_habit_version_can_only_be_withdrawn(db):
    draft_habit(db)
    approve_habit(db)
    db.execute(
        "UPDATE hq.habit_versions SET withdrawn_at=now(),withdrawn_reason='safety' WHERE habit_id='test-habit'"
    )
    assert db.execute(
        "SELECT withdrawn_reason FROM hq.habit_versions WHERE habit_id='test-habit'"
    ).fetchone() == ("safety",)


def test_draft_details_remain_editable(db):
    draft_habit(db)
    db.execute("UPDATE hq.habit_levels SET minutes=7 WHERE habit_id='test-habit'")
    db.execute("DELETE FROM hq.habit_levels WHERE habit_id='test-habit'")
    db.execute("DELETE FROM hq.habit_versions WHERE habit_id='test-habit'")
    assert db.execute("SELECT count(*) FROM hq.habit_versions WHERE habit_id='test-habit'").fetchone() == (0,)


def test_author_cannot_approve_own_version(db):
    draft_habit(db)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "UPDATE hq.habit_versions SET status='approved',reviewed_by=created_by,reviewed_at=now()"
        )


@pytest.mark.parametrize(
    "setup,statement",
    [
        (
            "INSERT INTO hq.goals(id,version,title,created_by) VALUES('test-goal',1,'Goal',%(a)s)",
            "UPDATE hq.goals SET title='Changed' WHERE id='test-goal'",
        ),
        (
            "INSERT INTO hq.concerns(id,version,label,created_by) VALUES('test-concern',1,'Concern',%(a)s)",
            "INSERT INTO hq.concern_goals VALUES('test-concern',1,'test-goal',1)",
        ),
        (
            "INSERT INTO hq.chapter_templates(id,version,world,goal_id,title,created_by) "
            "VALUES('test-chapter',1,'space','test-goal','Chapter',%(a)s)",
            "UPDATE hq.chapter_templates SET title='Changed' WHERE id='test-chapter'",
        ),
    ],
)
def test_other_approved_entities_are_immutable(db, setup, statement):
    db.execute(setup, {"a": AUTHOR})
    table = setup.split()[2].split("(")[0]
    db.execute(f"UPDATE {table} SET status='approved',reviewed_by=%s,reviewed_at=now()", (REVIEWER,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute(statement)


def test_draft_concern_goals_are_editable(db):
    db.execute("INSERT INTO hq.concerns(id,version,label,created_by) VALUES('test-concern',1,'Concern',%s)", (AUTHOR,))
    db.execute("INSERT INTO hq.concern_goals VALUES('test-concern',1,'test-goal',1)")
    db.execute("DELETE FROM hq.concern_goals WHERE concern_id='test-concern'")


def test_single_current_release_and_frozen_published_items(db):
    db.execute("INSERT INTO hq.catalog_releases(id,built_by) VALUES(10,%s),(11,%s)", (AUTHOR, AUTHOR))
    db.execute("INSERT INTO hq.release_items VALUES(11,'habit','x',1)")
    db.execute("DELETE FROM hq.release_items WHERE release_id=11")
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE hq.catalog_releases SET published_by=built_by,published_at=now() WHERE id=10")
    db.execute("UPDATE hq.catalog_releases SET is_current=false")
    db.execute(
        "UPDATE hq.catalog_releases SET published_by=%s,published_at=now() WHERE id IN (10,11)", (REVIEWER,)
    )
    db.execute("UPDATE hq.catalog_releases SET is_current=true WHERE id=10")
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute("UPDATE hq.catalog_releases SET is_current=true WHERE id=11")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute("INSERT INTO hq.release_items VALUES(10,'habit','x',1)")


@pytest.mark.parametrize("role", ["hq_api", "hq_worker"])
def test_service_roles_cannot_read_catalog_tables(db, role):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with db.transaction():
            db.execute(f"SET LOCAL ROLE {role}")
            db.execute("SELECT 1 FROM hq.habit_versions")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_catalog_schema.py -q --no-cov`
Expected: FAIL with `relation "hq.habits" does not exist` (and similar).

- [ ] **Step 3: Write the migration**

Create `supabase/migrations/202610290001_catalog.sql`:

```sql
-- Versioned, two-person-reviewed habit catalog with numbered releases.
-- Coexists with hq.catalog/hq.chapters until planning switches over (plan 2).

CREATE FUNCTION hq.guard_approved_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.status <> 'approved' THEN
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
  END IF;
  -- Withdrawal is the only change allowed after approval.
  IF TG_OP = 'DELETE' OR (to_jsonb(NEW) - 'withdrawn_at' - 'withdrawn_reason')
       IS DISTINCT FROM (to_jsonb(OLD) - 'withdrawn_at' - 'withdrawn_reason') THEN
    RAISE EXCEPTION 'Approved catalog versions are immutable' USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;

CREATE FUNCTION hq.guard_habit_detail() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP <> 'INSERT' AND EXISTS(SELECT 1 FROM hq.habit_versions v
      WHERE v.habit_id = OLD.habit_id AND v.version = OLD.version AND v.status = 'approved') THEN
    RAISE EXCEPTION 'Approved catalog versions are immutable' USING ERRCODE = '42501';
  END IF;
  IF TG_OP <> 'DELETE' AND EXISTS(SELECT 1 FROM hq.habit_versions v
      WHERE v.habit_id = NEW.habit_id AND v.version = NEW.version AND v.status = 'approved') THEN
    RAISE EXCEPTION 'Approved catalog versions are immutable' USING ERRCODE = '42501';
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;

CREATE FUNCTION hq.guard_concern_detail() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP <> 'INSERT' AND EXISTS(SELECT 1 FROM hq.concerns c
      WHERE c.id = OLD.concern_id AND c.version = OLD.concern_version AND c.status = 'approved') THEN
    RAISE EXCEPTION 'Approved catalog versions are immutable' USING ERRCODE = '42501';
  END IF;
  IF TG_OP <> 'DELETE' AND EXISTS(SELECT 1 FROM hq.concerns c
      WHERE c.id = NEW.concern_id AND c.version = NEW.concern_version AND c.status = 'approved') THEN
    RAISE EXCEPTION 'Approved catalog versions are immutable' USING ERRCODE = '42501';
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;

CREATE FUNCTION hq.guard_release_items() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP <> 'INSERT' AND EXISTS(SELECT 1 FROM hq.catalog_releases r
      WHERE r.id = OLD.release_id AND r.published_at IS NOT NULL) THEN
    RAISE EXCEPTION 'Published releases are immutable' USING ERRCODE = '42501';
  END IF;
  IF TG_OP <> 'DELETE' AND EXISTS(SELECT 1 FROM hq.catalog_releases r
      WHERE r.id = NEW.release_id AND r.published_at IS NOT NULL) THEN
    RAISE EXCEPTION 'Published releases are immutable' USING ERRCODE = '42501';
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;

CREATE TABLE hq.goals(
  id text NOT NULL CHECK(id ~ '^[a-z][a-z0-9-]*$'), version int NOT NULL CHECK(version > 0),
  title text NOT NULL, description text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','in_review','approved','rejected')),
  created_by uuid NOT NULL, reviewed_by uuid, reviewed_at timestamptz,
  CHECK(status <> 'approved' OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL AND reviewed_by <> created_by)),
  PRIMARY KEY(id, version));

CREATE TABLE hq.concerns(
  id text NOT NULL CHECK(id ~ '^[a-z][a-z0-9-]*$'), version int NOT NULL CHECK(version > 0),
  label text NOT NULL, synonyms text[] NOT NULL DEFAULT '{}',
  status text NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','in_review','approved','rejected')),
  created_by uuid NOT NULL, reviewed_by uuid, reviewed_at timestamptz,
  CHECK(status <> 'approved' OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL AND reviewed_by <> created_by)),
  PRIMARY KEY(id, version));

CREATE TABLE hq.concern_goals(
  concern_id text NOT NULL, concern_version int NOT NULL, goal_id text NOT NULL,
  weight numeric(3,2) NOT NULL CHECK(weight > 0 AND weight <= 1),
  PRIMARY KEY(concern_id, concern_version, goal_id),
  FOREIGN KEY(concern_id, concern_version) REFERENCES hq.concerns ON DELETE CASCADE);

CREATE TABLE hq.habits(id text PRIMARY KEY CHECK(id ~ '^[a-z][a-z0-9-]*$'));

CREATE TABLE hq.habit_versions(
  habit_id text NOT NULL REFERENCES hq.habits, version int NOT NULL CHECK(version > 0),
  title text NOT NULL, instruction text NOT NULL,
  min_age int NOT NULL CHECK(min_age BETWEEN 6 AND 13), max_age int NOT NULL CHECK(max_age BETWEEN 6 AND 13),
  routine_anchor text NOT NULL CHECK(routine_anchor IN ('morning','after_school','evening','bedtime')),
  safety_notes text NOT NULL DEFAULT '', withdrawn_at timestamptz, withdrawn_reason text,
  status text NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','in_review','approved','rejected')),
  created_by uuid NOT NULL, reviewed_by uuid, reviewed_at timestamptz,
  CHECK(status <> 'approved' OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL AND reviewed_by <> created_by)),
  CHECK(min_age <= max_age),
  CHECK((withdrawn_at IS NULL) = (withdrawn_reason IS NULL)),
  PRIMARY KEY(habit_id, version));

CREATE TABLE hq.habit_levels(
  habit_id text NOT NULL, version int NOT NULL,
  level text NOT NULL CHECK(level IN ('simple','standard','stretch')),
  minutes int NOT NULL CHECK(minutes BETWEEN 1 AND 60), points int NOT NULL CHECK(points BETWEEN 1 AND 3),
  instruction text,
  PRIMARY KEY(habit_id, version, level),
  FOREIGN KEY(habit_id, version) REFERENCES hq.habit_versions ON DELETE CASCADE);

CREATE TABLE hq.habit_goals(
  habit_id text NOT NULL, version int NOT NULL, goal_id text NOT NULL,
  strength numeric(3,2) NOT NULL CHECK(strength > 0 AND strength <= 1),
  PRIMARY KEY(habit_id, version, goal_id),
  FOREIGN KEY(habit_id, version) REFERENCES hq.habit_versions ON DELETE CASCADE);

CREATE TABLE hq.habit_prereqs(
  habit_id text NOT NULL, version int NOT NULL, supports_habit_id text NOT NULL REFERENCES hq.habits,
  kind text NOT NULL CHECK(kind IN ('prepares')),
  CHECK(supports_habit_id <> habit_id),
  PRIMARY KEY(habit_id, version, supports_habit_id),
  FOREIGN KEY(habit_id, version) REFERENCES hq.habit_versions ON DELETE CASCADE);

CREATE TABLE hq.habit_tags(
  habit_id text NOT NULL, version int NOT NULL,
  tag text NOT NULL CHECK(tag ~ '^(interest|topic|access):[a-z_]+$'),
  PRIMARY KEY(habit_id, version, tag),
  FOREIGN KEY(habit_id, version) REFERENCES hq.habit_versions ON DELETE CASCADE);

CREATE TABLE hq.habit_variants(
  habit_id text NOT NULL, version int NOT NULL, interest text NOT NULL CHECK(interest ~ '^[a-z_]+$'),
  title text NOT NULL, instruction text NOT NULL,
  PRIMARY KEY(habit_id, version, interest),
  FOREIGN KEY(habit_id, version) REFERENCES hq.habit_versions ON DELETE CASCADE);

CREATE TABLE hq.chapter_templates(
  id text NOT NULL CHECK(id ~ '^[a-z][a-z0-9-]*$'), version int NOT NULL CHECK(version > 0),
  world text NOT NULL CHECK(world IN ('space','forest','city')), goal_id text NOT NULL, title text NOT NULL,
  status text NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','in_review','approved','rejected')),
  created_by uuid NOT NULL, reviewed_by uuid, reviewed_at timestamptz,
  CHECK(status <> 'approved' OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL AND reviewed_by <> created_by)),
  PRIMARY KEY(id, version));

CREATE TABLE hq.catalog_releases(
  id int PRIMARY KEY CHECK(id > 0), built_by uuid NOT NULL, published_by uuid, published_at timestamptz,
  notes text NOT NULL DEFAULT '', is_current boolean NOT NULL DEFAULT false,
  CHECK((published_by IS NULL) = (published_at IS NULL)),
  CHECK(published_by IS NULL OR published_by <> built_by),
  CHECK(NOT is_current OR published_at IS NOT NULL));
CREATE UNIQUE INDEX one_current_release ON hq.catalog_releases(is_current) WHERE is_current;

CREATE TABLE hq.release_items(
  release_id int NOT NULL REFERENCES hq.catalog_releases,
  entity text NOT NULL CHECK(entity IN ('goal','concern','habit','chapter')),
  entity_id text NOT NULL, version int NOT NULL,
  PRIMARY KEY(release_id, entity, entity_id));

CREATE TRIGGER immutable_approved BEFORE UPDATE OR DELETE ON hq.goals
  FOR EACH ROW EXECUTE FUNCTION hq.guard_approved_version();
CREATE TRIGGER immutable_approved BEFORE UPDATE OR DELETE ON hq.concerns
  FOR EACH ROW EXECUTE FUNCTION hq.guard_approved_version();
CREATE TRIGGER immutable_approved BEFORE UPDATE OR DELETE ON hq.habit_versions
  FOR EACH ROW EXECUTE FUNCTION hq.guard_approved_version();
CREATE TRIGGER immutable_approved BEFORE UPDATE OR DELETE ON hq.chapter_templates
  FOR EACH ROW EXECUTE FUNCTION hq.guard_approved_version();
CREATE TRIGGER immutable_approved BEFORE INSERT OR UPDATE OR DELETE ON hq.concern_goals
  FOR EACH ROW EXECUTE FUNCTION hq.guard_concern_detail();
CREATE TRIGGER immutable_approved BEFORE INSERT OR UPDATE OR DELETE ON hq.release_items
  FOR EACH ROW EXECUTE FUNCTION hq.guard_release_items();
DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['habit_levels','habit_goals','habit_prereqs','habit_tags','habit_variants'] LOOP
    EXECUTE format('CREATE TRIGGER immutable_approved BEFORE INSERT OR UPDATE OR DELETE ON hq.%I '
                   'FOR EACH ROW EXECUTE FUNCTION hq.guard_habit_detail()', t);
  END LOOP;
END $$;

-- Service roles read only published, approved content through these views.
CREATE VIEW hq.current_release AS SELECT id FROM hq.catalog_releases WHERE is_current;

CREATE VIEW hq.released_goals AS
SELECT i.release_id, g.id AS goal_id, g.version, g.title, g.description
FROM hq.release_items i
JOIN hq.catalog_releases r ON r.id = i.release_id AND r.published_at IS NOT NULL
JOIN hq.goals g ON i.entity = 'goal' AND g.id = i.entity_id AND g.version = i.version AND g.status = 'approved';

CREATE VIEW hq.released_concerns AS
SELECT i.release_id, c.id AS concern_id, c.version, c.label, c.synonyms,
  (SELECT coalesce(jsonb_object_agg(cg.goal_id, cg.weight), '{}'::jsonb) FROM hq.concern_goals cg
   WHERE cg.concern_id = c.id AND cg.concern_version = c.version) AS goals
FROM hq.release_items i
JOIN hq.catalog_releases r ON r.id = i.release_id AND r.published_at IS NOT NULL
JOIN hq.concerns c ON i.entity = 'concern' AND c.id = i.entity_id AND c.version = i.version AND c.status = 'approved';

CREATE VIEW hq.released_chapters AS
SELECT i.release_id, t.id AS chapter_id, t.version, t.world, t.goal_id, t.title
FROM hq.release_items i
JOIN hq.catalog_releases r ON r.id = i.release_id AND r.published_at IS NOT NULL
JOIN hq.chapter_templates t ON i.entity = 'chapter' AND t.id = i.entity_id AND t.version = i.version
  AND t.status = 'approved';

CREATE VIEW hq.released_habits AS
SELECT i.release_id, v.habit_id, v.version, v.title, v.instruction, v.min_age, v.max_age, v.routine_anchor,
  v.withdrawn_at IS NOT NULL AS withdrawn,
  (SELECT coalesce(jsonb_agg(jsonb_build_object('level', l.level, 'minutes', l.minutes, 'points', l.points,
            'instruction', l.instruction) ORDER BY array_position(ARRAY['simple','standard','stretch'], l.level)),
          '[]'::jsonb)
   FROM hq.habit_levels l WHERE l.habit_id = v.habit_id AND l.version = v.version) AS levels,
  (SELECT coalesce(jsonb_object_agg(g.goal_id, g.strength), '{}'::jsonb)
   FROM hq.habit_goals g WHERE g.habit_id = v.habit_id AND g.version = v.version) AS goals,
  (SELECT coalesce(array_agg(t.tag ORDER BY t.tag), '{}'::text[])
   FROM hq.habit_tags t WHERE t.habit_id = v.habit_id AND t.version = v.version) AS tags,
  (SELECT coalesce(jsonb_object_agg(x.interest, jsonb_build_object('title', x.title, 'instruction', x.instruction)),
          '{}'::jsonb)
   FROM hq.habit_variants x WHERE x.habit_id = v.habit_id AND x.version = v.version) AS variants,
  (SELECT coalesce(jsonb_object_agg(p.supports_habit_id, p.kind), '{}'::jsonb)
   FROM hq.habit_prereqs p WHERE p.habit_id = v.habit_id AND p.version = v.version) AS supports
FROM hq.release_items i
JOIN hq.catalog_releases r ON r.id = i.release_id AND r.published_at IS NOT NULL
JOIN hq.habit_versions v ON i.entity = 'habit' AND v.habit_id = i.entity_id AND v.version = i.version
  AND v.status = 'approved';

GRANT SELECT ON hq.current_release, hq.released_goals, hq.released_concerns, hq.released_chapters,
  hq.released_habits TO hq_api, hq_worker;
```

- [ ] **Step 4: Make fixtures reset the new tables**

In `tests/conftest.py`, directly after line 61 (`conn.execute("TRUNCATE hq.catalog,hq.chapters,hq.policy")`), add:

```python
        conn.execute(
            "TRUNCATE hq.release_items,hq.catalog_releases,hq.habit_variants,hq.habit_tags,hq.habit_prereqs,"
            "hq.habit_goals,hq.habit_levels,hq.habit_versions,hq.habits,hq.concern_goals,hq.concerns,"
            "hq.goals,hq.chapter_templates"
        )
```

`TRUNCATE` does not fire the row-level guard triggers, so fixtures can reset approved rows.

- [ ] **Step 5: Apply the migration and run the tests**

```powershell
$env:DATABASE_URL = (Select-String -Path .env -Pattern '^TEST_DATABASE_URL=(.*)$').Matches[0].Groups[1].Value
uv run python scripts/setup_db.py
uv run python -m pytest tests/test_catalog_schema.py -q --no-cov
```

Expected: `15 passed`.

- [ ] **Step 6: Run the full suite**

Run: `uv run python -m pytest -q`
Expected: all tests pass, coverage 100% (no Python source changed yet).

- [ ] **Step 7: Commit**

```bash
git add supabase/migrations/202610290001_catalog.sql tests/conftest.py tests/test_catalog_schema.py
git commit -m "feat(catalog): add versioned catalog schema, immutability guards and released views"
```

---

### Task 2: Seed release 1

**Files:**
- Modify: `supabase/seed.sql` (append)
- Test: `tests/test_catalog_schema.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_catalog_schema.py`:

```python
GOALS = {"bedtime", "morning", "bedtime-mornings", "meals", "responsibility"}


def test_release_one_is_current_and_complete(db):
    assert db.execute("SELECT id FROM hq.current_release").fetchall() == [(1,)]
    habits = dict(db.execute("SELECT habit_id, goals FROM hq.released_habits WHERE release_id=1").fetchall())
    assert set(habits) == {"bedtime-prep", "morning-ready", "join-meal", "clear-plate", "pack-bag"}
    assert habits["clear-plate"] == {"responsibility": 1.0, "meals": 0.5}
    goals = {row[0] for row in db.execute("SELECT goal_id FROM hq.released_goals WHERE release_id=1")}
    assert goals == GOALS
    chapters = db.execute(
        "SELECT goal_id, count(*) FROM hq.released_chapters WHERE release_id=1 GROUP BY goal_id"
    ).fetchall()
    assert dict(chapters) == {goal: 3 for goal in GOALS}
    concern_goals = {
        goal for (row,) in db.execute("SELECT goals FROM hq.released_concerns WHERE release_id=1") for goal in row
    }
    assert {goal for row in habits.values() for goal in row} | concern_goals <= GOALS


@pytest.mark.parametrize("role", ["hq_api", "hq_worker"])
def test_service_roles_see_only_published_approved_habits(db, role):
    draft_habit(db, "draft-habit")
    db.execute("INSERT INTO hq.catalog_releases(id,built_by) VALUES(2,%s)", (AUTHOR,))
    db.execute("INSERT INTO hq.release_items VALUES(2,'habit','draft-habit',1),(2,'habit','bedtime-prep',1)")
    with db.transaction():
        db.execute(f"SET LOCAL ROLE {role}")
        rows = db.execute("SELECT release_id, habit_id FROM hq.released_habits").fetchall()
    assert {release for release, _ in rows} == {1}
    assert "draft-habit" not in {habit for _, habit in rows}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_catalog_schema.py -q --no-cov -k "release_one or published_approved"`
Expected: FAIL; `current_release` returns `[]`.

- [ ] **Step 3: Append the seed**

Append to `supabase/seed.sql`:

```sql
-- Release 1: the development catalog migrated into the versioned model.
-- Development fixture only; not reviewed for a family pilot.
DO $$
DECLARE
  author uuid := '00000000-0000-0000-0000-0000000000a1';
  reviewer uuid := '00000000-0000-0000-0000-0000000000a2';
BEGIN
  IF EXISTS(SELECT 1 FROM hq.catalog_releases WHERE id = 1) THEN RETURN; END IF;

  INSERT INTO hq.goals(id, version, title, created_by) VALUES
    ('bedtime', 1, 'Build a consistent bedtime preparation routine', author),
    ('morning', 1, 'Be ready for the day with fewer reminders', author),
    ('bedtime-mornings', 1, 'Predictable bedtime and mornings', author),
    ('meals', 1, 'Join the after-school meal routine', author),
    ('responsibility', 1, 'Care for belongings and shared spaces', author);

  INSERT INTO hq.concerns(id, version, label, synonyms, created_by) VALUES
    ('bedtime-reminders', 1, 'Bedtime takes many reminders',
     ARRAY['won''t go to bed', 'stalls at bedtime', 'bedtime battles'], author),
    ('rushed-mornings', 1, 'Mornings are rushed',
     ARRAY['late for school', 'slow getting ready', 'morning stress'], author),
    ('meal-joining', 1, 'Hard to come to the table',
     ARRAY['won''t come to eat', 'skips the meal routine'], author),
    ('belongings-left', 1, 'Belongings and dishes are left around',
     ARRAY['plates left out', 'school things everywhere', 'messy shared spaces'], author);
  INSERT INTO hq.concern_goals VALUES
    ('bedtime-reminders', 1, 'bedtime', 1), ('bedtime-reminders', 1, 'bedtime-mornings', 0.6),
    ('rushed-mornings', 1, 'morning', 1), ('rushed-mornings', 1, 'bedtime-mornings', 0.6),
    ('meal-joining', 1, 'meals', 1),
    ('belongings-left', 1, 'responsibility', 1);

  INSERT INTO hq.habits VALUES ('bedtime-prep'), ('morning-ready'), ('join-meal'), ('clear-plate'), ('pack-bag');
  INSERT INTO hq.habit_versions(habit_id, version, title, instruction, min_age, max_age, routine_anchor, created_by)
  VALUES
    ('bedtime-prep', 1, 'Begin bedtime preparation',
     'At your agreed cue, put on pajamas and brush your teeth.', 6, 13, 'bedtime', author),
    ('morning-ready', 1, 'Follow the morning checklist',
     'At your agreed cue, get dressed and prepare for the day.', 6, 13, 'morning', author),
    ('join-meal', 1, 'Join the after-school meal',
     'Come to the table at your agreed meal cue. You do not have to finish a prescribed amount.',
     6, 13, 'after_school', author),
    ('clear-plate', 1, 'Clear your plate', 'After eating, put your plate in the agreed place.',
     6, 13, 'after_school', author),
    ('pack-bag', 1, 'Prepare your school bag', 'Put the agreed school items in your bag for tomorrow.',
     6, 13, 'evening', author);
  INSERT INTO hq.habit_levels(habit_id, version, level, minutes, points) VALUES
    ('bedtime-prep', 1, 'standard', 10, 1), ('morning-ready', 1, 'standard', 10, 1),
    ('join-meal', 1, 'standard', 5, 1), ('clear-plate', 1, 'standard', 2, 1), ('pack-bag', 1, 'standard', 5, 1);
  INSERT INTO hq.habit_goals VALUES
    ('bedtime-prep', 1, 'bedtime', 1), ('bedtime-prep', 1, 'bedtime-mornings', 0.8),
    ('morning-ready', 1, 'morning', 1), ('morning-ready', 1, 'bedtime-mornings', 0.8),
    ('join-meal', 1, 'meals', 1),
    ('clear-plate', 1, 'responsibility', 1), ('clear-plate', 1, 'meals', 0.5),
    ('pack-bag', 1, 'responsibility', 0.8), ('pack-bag', 1, 'morning', 0.5);
  INSERT INTO hq.habit_prereqs VALUES ('pack-bag', 1, 'morning-ready', 'prepares');
  INSERT INTO hq.habit_tags
  SELECT habit_id, 1, 'access:short_instructions' FROM hq.habits
  UNION ALL VALUES ('bedtime-prep', 1, 'interest:space'), ('pack-bag', 1, 'interest:space');
  INSERT INTO hq.habit_variants VALUES
    ('bedtime-prep', 1, 'space', 'Prepare for Night Docking',
     'At the agreed cue, put on pajamas and brush your teeth.'),
    ('pack-bag', 1, 'space', 'Pack Your Mission Kit', 'Put the agreed school items in your bag for tomorrow.');

  INSERT INTO hq.chapter_templates(id, version, world, goal_id, title, created_by)
  SELECT world || '-' || goal, 1, world, goal, title, author FROM (VALUES
    ('space', 'bedtime', 'Prepare the Home Base'), ('space', 'morning', 'Launch the Day'),
    ('space', 'meals', 'Join the Crew'), ('space', 'responsibility', 'Prepare Your Equipment'),
    ('space', 'bedtime-mornings', 'Night Docking to Launch'),
    ('forest', 'bedtime', 'Settle at the Treehouse'), ('forest', 'morning', 'Start the Woodland Journey'),
    ('forest', 'meals', 'Gather at the Table'), ('forest', 'responsibility', 'Prepare Your Trail Pack'),
    ('forest', 'bedtime-mornings', 'Treehouse to Trail'),
    ('city', 'bedtime', 'Settle at Home'), ('city', 'morning', 'Start the City Day'),
    ('city', 'meals', 'Join the Neighborhood Table'), ('city', 'responsibility', 'Prepare Your Day Bag'),
    ('city', 'bedtime-mornings', 'Home to City Day')
  ) AS t(world, goal, title);

  UPDATE hq.goals SET status = 'approved', reviewed_by = reviewer, reviewed_at = now();
  UPDATE hq.concerns SET status = 'approved', reviewed_by = reviewer, reviewed_at = now();
  UPDATE hq.habit_versions SET status = 'approved', reviewed_by = reviewer, reviewed_at = now();
  UPDATE hq.chapter_templates SET status = 'approved', reviewed_by = reviewer, reviewed_at = now();

  INSERT INTO hq.catalog_releases(id, built_by, notes) VALUES (1, author, 'Development catalog migrated to releases');
  INSERT INTO hq.release_items
  SELECT 1, 'goal', id, version FROM hq.goals
  UNION ALL SELECT 1, 'concern', id, version FROM hq.concerns
  UNION ALL SELECT 1, 'habit', habit_id, version FROM hq.habit_versions
  UNION ALL SELECT 1, 'chapter', id, version FROM hq.chapter_templates;
  UPDATE hq.catalog_releases SET published_by = reviewer, published_at = now(), is_current = true WHERE id = 1;
END $$;
```

The seed inserts details while versions are drafts, then approves them, then publishes; the guards would otherwise reject it.

- [ ] **Step 4: Reseed the test database and run the tests**

```powershell
$env:DATABASE_URL = (Select-String -Path .env -Pattern '^TEST_DATABASE_URL=(.*)$').Matches[0].Groups[1].Value
uv run python scripts/setup_db.py
uv run python -m pytest tests/test_catalog_schema.py -q --no-cov
```

Expected: `18 passed`. Run `setup_db.py` a second time and confirm it succeeds (seed is idempotent).

- [ ] **Step 5: Run the full suite and commit**

Run: `uv run python -m pytest -q` — expected all pass, 100% coverage.

```bash
git add supabase/seed.sql tests/test_catalog_schema.py
git commit -m "feat(catalog): seed release 1 from the development catalog"
```

---

### Task 3: Catalog models and release repository

**Files:**
- Create: `src/habitquest_backend/catalog/__init__.py`
- Create: `src/habitquest_backend/catalog/models.py`
- Create: `src/habitquest_backend/catalog/repository.py`
- Create: `tests/catalog_factories.py`
- Test: `tests/test_catalog_repository.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_catalog_repository.py`:

```python
import pytest
from fastapi import HTTPException

from habitquest_backend.catalog.models import Level
from habitquest_backend.catalog.repository import load_release
from habitquest_backend.db import transaction


def test_load_current_release(settings, db):
    with transaction(settings, worker=True) as conn:
        release = load_release(conn)
    assert release.id == 1
    assert set(release.goals) == {"bedtime", "morning", "bedtime-mornings", "meals", "responsibility"}
    plate = release.habits["clear-plate"]
    assert plate.goals == {"responsibility": 1.0, "meals": 0.5}
    assert plate.levels == (Level("standard", 2, 1),)
    assert plate.routine_anchor == "after_school"
    assert plate.tags == frozenset({"access:short_instructions"})
    assert not plate.withdrawn
    bedtime = release.habits["bedtime-prep"]
    assert bedtime.variants["space"] == (
        "Prepare for Night Docking",
        "At the agreed cue, put on pajamas and brush your teeth.",
    )
    assert release.habits["pack-bag"].supports == {"morning-ready": "prepares"}
    assert release.concerns["belongings-left"].goals == {"responsibility": 1.0}
    assert "plates left out" in release.concerns["belongings-left"].synonyms
    assert release.chapters[("space", "bedtime")].title == "Prepare the Home Base"


def test_load_named_release_without_a_current_release(settings, db):
    db.execute("UPDATE hq.catalog_releases SET is_current=false")
    with transaction(settings, worker=True) as conn:
        assert load_release(conn, 1).id == 1
        with pytest.raises(HTTPException) as error:
            load_release(conn)
    assert error.value.status_code == 404


def test_presented_wording_prefers_matching_interest_variant(settings, db):
    with transaction(settings, worker=True) as conn:
        habit = load_release(conn).habits["bedtime-prep"]
    assert habit.presented(frozenset({"animals", "space"}))[0] == "Prepare for Night Docking"
    assert habit.presented(frozenset({"animals"})) == (habit.title, habit.instruction)
```

Create `tests/catalog_factories.py`:

```python
from habitquest_backend.catalog.models import Habit, Level, Release


def make_habit(
    habit_id="h",
    *,
    goals=None,
    min_age=6,
    max_age=13,
    anchor="bedtime",
    levels=None,
    tags=(),
    supports=None,
    variants=None,
    withdrawn=False,
):
    return Habit(
        id=habit_id,
        version=1,
        title=f"Title {habit_id}",
        instruction=f"Do {habit_id}.",
        min_age=min_age,
        max_age=max_age,
        routine_anchor=anchor,
        levels=tuple(levels or (Level("standard", 5, 1),)),
        goals=goals if goals is not None else {"g": 1.0},
        tags=frozenset(tags),
        variants=variants or {},
        supports=supports or {},
        withdrawn=withdrawn,
    )


def make_release(*habits):
    return Release(id=1, goals={}, concerns={}, habits={h.id: h for h in habits}, chapters={})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_catalog_repository.py -q --no-cov`
Expected: FAIL with `ModuleNotFoundError: No module named 'habitquest_backend.catalog'`.

- [ ] **Step 3: Write the models**

Create `src/habitquest_backend/catalog/__init__.py`:

```python
"""Versioned, reviewed habit catalog: release loading, eligibility and scoring."""
```

Create `src/habitquest_backend/catalog/models.py`:

```python
from dataclasses import dataclass, field
from typing import Literal

Anchor = Literal["morning", "after_school", "evening", "bedtime"]
LevelName = Literal["simple", "standard", "stretch"]
AgeBand = Literal["6-9", "10-13"]
LEVEL_ORDER: tuple[LevelName, ...] = ("simple", "standard", "stretch")


@dataclass(frozen=True)
class Level:
    level: LevelName
    minutes: int
    points: int
    instruction: str | None = None


@dataclass(frozen=True)
class Habit:
    id: str
    version: int
    title: str
    instruction: str
    min_age: int
    max_age: int
    routine_anchor: Anchor
    levels: tuple[Level, ...]
    goals: dict[str, float]
    tags: frozenset[str]
    variants: dict[str, tuple[str, str]]
    supports: dict[str, str]
    withdrawn: bool = False

    def presented(self, interests: frozenset[str]) -> tuple[str, str]:
        """Reviewed child-facing title and instruction; interest variants never change the activity."""
        for interest in sorted(interests):
            if interest in self.variants:
                return self.variants[interest]
        return self.title, self.instruction


@dataclass(frozen=True)
class Goal:
    id: str
    version: int
    title: str
    description: str


@dataclass(frozen=True)
class Concern:
    id: str
    version: int
    label: str
    synonyms: tuple[str, ...]
    goals: dict[str, float]


@dataclass(frozen=True)
class Chapter:
    id: str
    version: int
    world: str
    goal_id: str
    title: str


@dataclass(frozen=True)
class Release:
    id: int
    goals: dict[str, Goal]
    concerns: dict[str, Concern]
    habits: dict[str, Habit]
    chapters: dict[tuple[str, str], Chapter]


@dataclass(frozen=True)
class RoutineSlot:
    cue: str
    days: tuple[str, ...]


@dataclass(frozen=True)
class ChildProfile:
    age_band: AgeBand
    interests: frozenset[str] = frozenset()
    accessibility: frozenset[str] = frozenset()
    opted_in_topics: frozenset[str] = frozenset()
    routine_schedule: dict[str, RoutineSlot] = field(default_factory=dict)
```

- [ ] **Step 4: Write the repository**

Create `src/habitquest_backend/catalog/repository.py`:

```python
from typing import Any

from habitquest_backend.catalog.models import Chapter, Concern, Goal, Habit, Level, Release
from habitquest_backend.db import one


def load_release(conn: Any, release_id: int | None = None) -> Release:
    """Load a published release through the read-only released_* views."""
    if release_id is None:
        release_id = one(conn, "SELECT id FROM hq.current_release")["id"]
    params = (release_id,)
    goals = {
        row["goal_id"]: Goal(row["goal_id"], row["version"], row["title"], row["description"])
        for row in conn.execute("SELECT * FROM hq.released_goals WHERE release_id=%s", params)
    }
    concerns = {
        row["concern_id"]: Concern(
            row["concern_id"],
            row["version"],
            row["label"],
            tuple(row["synonyms"]),
            {goal: float(weight) for goal, weight in row["goals"].items()},
        )
        for row in conn.execute("SELECT * FROM hq.released_concerns WHERE release_id=%s", params)
    }
    habits = {
        row["habit_id"]: Habit(
            id=row["habit_id"],
            version=row["version"],
            title=row["title"],
            instruction=row["instruction"],
            min_age=row["min_age"],
            max_age=row["max_age"],
            routine_anchor=row["routine_anchor"],
            levels=tuple(Level(**level) for level in row["levels"]),
            goals={goal: float(strength) for goal, strength in row["goals"].items()},
            tags=frozenset(row["tags"]),
            variants={k: (v["title"], v["instruction"]) for k, v in row["variants"].items()},
            supports=dict(row["supports"]),
            withdrawn=row["withdrawn"],
        )
        for row in conn.execute("SELECT * FROM hq.released_habits WHERE release_id=%s", params)
    }
    chapters = {
        (row["world"], row["goal_id"]): Chapter(
            row["chapter_id"], row["version"], row["world"], row["goal_id"], row["title"]
        )
        for row in conn.execute("SELECT * FROM hq.released_chapters WHERE release_id=%s", params)
    }
    return Release(release_id, goals, concerns, habits, chapters)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_catalog_repository.py -q --no-cov`
Expected: `3 passed`.

- [ ] **Step 6: Lint, type-check and commit**

```powershell
uv run ruff format src tests
uv run ruff check --fix src tests
uv run python -m mypy src
uv run python -m pytest -q
```

Expected: no lint or type errors; all tests pass; coverage 100%.

```bash
git add src/habitquest_backend/catalog tests/catalog_factories.py tests/test_catalog_repository.py
git commit -m "feat(catalog): load published releases into frozen catalog models"
```

---

### Task 4: Eligibility

**Files:**
- Create: `src/habitquest_backend/catalog/eligibility.py`
- Test: `tests/test_catalog_eligibility.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_catalog_eligibility.py`:

```python
from catalog_factories import make_habit, make_release

from habitquest_backend.catalog.eligibility import eligible
from habitquest_backend.catalog.models import ChildProfile, Level, RoutineSlot

SCHEDULE = {"bedtime": RoutineSlot("20:00", ("mon", "tue"))}
PROFILE = ChildProfile(age_band="6-9", routine_schedule=SCHEDULE)


def ids(candidates):
    return [c.habit.id for c in candidates]


def test_keeps_matching_habit_with_served_goals_in_priority_order():
    habit = make_habit("a", goals={"x": 1.0, "y": 0.5})
    [candidate] = eligible(make_release(habit), PROFILE, ["y", "z", "x"], available_minutes=10)
    assert candidate.goal_ids == ("y", "x")
    assert candidate.level == Level("standard", 5, 1)
    assert not candidate.needs_cue


def test_excludes_withdrawn_unrelated_active_and_age_mismatched_habits():
    release = make_release(
        make_habit("withdrawn", withdrawn=True),
        make_habit("other-goal", goals={"other": 1.0}),
        make_habit("active"),
        make_habit("too-young", min_age=8),
        make_habit("too-old", max_age=8),
        make_habit("ok"),
    )
    result = eligible(release, PROFILE, ["g"], active_habit_ids=frozenset({"active"}), available_minutes=10)
    assert ids(result) == ["ok"]


def test_topic_tags_require_parent_opt_in():
    release = make_release(make_habit("puberty", tags={"topic:puberty_hygiene"}))
    assert eligible(release, PROFILE, ["g"], available_minutes=10) == []
    opted = ChildProfile(age_band="6-9", opted_in_topics=frozenset({"puberty_hygiene"}), routine_schedule=SCHEDULE)
    assert ids(eligible(release, opted, ["g"], available_minutes=10)) == ["puberty"]


def test_every_accessibility_need_must_be_supported():
    release = make_release(
        make_habit("short", tags={"access:short_instructions"}),
        make_habit("plain"),
    )
    needs = ChildProfile(age_band="6-9", accessibility=frozenset({"short_instructions"}), routine_schedule=SCHEDULE)
    assert ids(eligible(release, needs, ["g"], available_minutes=10)) == ["short"]


def test_chooses_simplest_level_that_fits_or_excludes():
    levels = (Level("stretch", 8, 3), Level("simple", 6, 1), Level("standard", 4, 2))
    release = make_release(make_habit("a", levels=levels))
    assert eligible(release, PROFILE, ["g"], available_minutes=5)[0].level.level == "standard"
    assert eligible(release, PROFILE, ["g"], available_minutes=6)[0].level.level == "simple"
    assert eligible(release, PROFILE, ["g"], available_minutes=3) == []


def test_marks_habits_without_an_agreed_cue():
    release = make_release(make_habit("m", anchor="morning"), make_habit("b"))
    result = {c.habit.id: c.needs_cue for c in eligible(release, PROFILE, ["g"], available_minutes=10)}
    assert result == {"b": False, "m": True}


def test_10_to_13_band_uses_its_own_bounds():
    release = make_release(make_habit("young", max_age=9), make_habit("all"))
    older = ChildProfile(age_band="10-13", routine_schedule=SCHEDULE)
    assert ids(eligible(release, older, ["g"], available_minutes=10)) == ["all"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_catalog_eligibility.py -q --no-cov`
Expected: FAIL with `ModuleNotFoundError: No module named 'habitquest_backend.catalog.eligibility'`.

- [ ] **Step 3: Write the implementation**

Create `src/habitquest_backend/catalog/eligibility.py`:

```python
from dataclasses import dataclass

from habitquest_backend.catalog.models import LEVEL_ORDER, ChildProfile, Habit, Level, Release

AGE_BANDS = {"6-9": (6, 9), "10-13": (10, 13)}


@dataclass(frozen=True)
class Candidate:
    habit: Habit
    goal_ids: tuple[str, ...]
    level: Level
    needs_cue: bool


def eligible(
    release: Release,
    profile: ChildProfile,
    goal_ids: list[str],
    *,
    available_minutes: int,
    active_habit_ids: frozenset[str] = frozenset(),
) -> list[Candidate]:
    """Deterministic hard filter; the LLM and scoring only ever see these candidates."""
    low, high = AGE_BANDS[profile.age_band]
    required_access = {f"access:{need}" for need in profile.accessibility}
    result = []
    for habit in sorted(release.habits.values(), key=lambda h: h.id):
        served = tuple(goal for goal in goal_ids if goal in habit.goals)
        topics = {tag.removeprefix("topic:") for tag in habit.tags if tag.startswith("topic:")}
        if (
            habit.withdrawn
            or not served
            or habit.id in active_habit_ids
            or not (habit.min_age <= low and habit.max_age >= high)
            or not topics <= profile.opted_in_topics
            or not required_access <= habit.tags
        ):
            continue
        levels = sorted(habit.levels, key=lambda level: LEVEL_ORDER.index(level.level))
        level = next((lv for lv in levels if lv.minutes <= available_minutes), None)
        if level is None:
            continue
        result.append(Candidate(habit, served, level, habit.routine_anchor not in profile.routine_schedule))
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_catalog_eligibility.py -q --no-cov`
Expected: `7 passed`.

- [ ] **Step 5: Lint, type-check, full suite and commit**

```powershell
uv run ruff format src tests
uv run ruff check --fix src tests
uv run python -m mypy src
uv run python -m pytest -q
```

Expected: clean; all pass; coverage 100%.

```bash
git add src/habitquest_backend/catalog/eligibility.py tests/test_catalog_eligibility.py
git commit -m "feat(catalog): add deterministic habit eligibility"
```

---

### Task 5: Scoring, shortlists and the template plan

**Files:**
- Create: `src/habitquest_backend/catalog/scoring.py`
- Test: `tests/test_catalog_scoring.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_catalog_scoring.py`:

```python
from catalog_factories import make_habit, make_release
from hypothesis import given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st

from habitquest_backend.catalog.eligibility import Candidate, eligible
from habitquest_backend.catalog.models import ChildProfile, Level, RoutineSlot
from habitquest_backend.catalog.scoring import score, shortlists, template_plan

PROFILE = ChildProfile(
    age_band="6-9",
    interests=frozenset({"space"}),
    routine_schedule={"bedtime": RoutineSlot("20:00", ("mon",)), "evening": RoutineSlot("19:00", ("mon",))},
)


def candidate(habit, goal_ids=("g",), level=None, needs_cue=False):
    return Candidate(habit, tuple(goal_ids), level or habit.levels[0], needs_cue)


def test_score_combines_priority_interest_prerequisite_and_level():
    bag = make_habit("bag", goals={"r": 0.8}, tags={"interest:space"}, supports={"morning": "prepares"})
    morning = make_habit("morning", goals={"m": 1.0}, levels=(Level("simple", 3, 1),))
    # Weights for ["m", "r"]: m=1.0, r=0.5. Bag: 0.5*0.8 + 0.5 + 0.3 - 0.1 = 1.1; morning: 1.0.
    [first, second] = score([candidate(bag, ("r",)), candidate(morning, ("m",))], ["m", "r"], PROFILE.interests)
    assert first.candidate.habit.id == "bag"
    assert first.breakdown == {"goal": 0.4, "interest": 0.5, "prereq": 0.3, "level": -0.1}
    assert first.score == 1.1
    assert second.breakdown == {"goal": 1.0, "interest": 0.0, "prereq": 0.0, "level": 0.0}
    assert second.score == 1.0


def test_ties_break_by_habit_id():
    result = score([candidate(make_habit("b")), candidate(make_habit("a"))], ["g"], frozenset())
    assert [s.candidate.habit.id for s in result] == ["a", "b"]


def test_shortlists_are_per_goal_and_capped():
    scored = score([candidate(make_habit(str(i))) for i in range(5)], ["g", "empty"], frozenset())
    lists = shortlists(scored, ["g", "empty"], size=3)
    assert [s.candidate.habit.id for s in lists["g"]] == ["0", "1", "2"]
    assert lists["empty"] == []


def test_template_plan_counts_multi_goal_habit_once_and_reports_reasons():
    plate = make_habit("plate", goals={"meals": 0.5, "resp": 1.0}, levels=(Level("standard", 2, 1),))
    morning = make_habit("morning", goals={"morning": 1.0}, anchor="morning")
    bag = make_habit("bag", goals={"resp": 0.8}, anchor="evening")
    release = make_release(plate, morning, bag)
    goals = ["resp", "meals", "morning", "bedtime"]
    candidates = eligible(release, PROFILE, goals, available_minutes=10)
    plan = template_plan(
        shortlists(score(candidates, goals, PROFILE.interests), goals),
        goals,
        max_active=3,
        available_minutes=10,
    )
    assert [s.candidate.habit.id for s in plan.active] == ["plate"]
    assert [s.candidate.habit.id for s in plan.future] == ["bag", "morning"]
    assert plan.unresolved == {"morning": "needs_cue", "bedtime": "no_catalog_entry"}


def test_template_plan_defers_goals_beyond_active_or_minute_limits():
    a = make_habit("a", goals={"x": 1.0}, levels=(Level("standard", 6, 1),))
    b = make_habit("b", goals={"y": 1.0}, levels=(Level("standard", 6, 1),))
    c = make_habit("c", goals={"z": 1.0}, levels=(Level("standard", 1, 1),))
    goals = ["x", "y", "z"]
    lists = shortlists(score([candidate(a, ("x",)), candidate(b, ("y",)), candidate(c, ("z",))], goals, frozenset()), goals)
    by_minutes = template_plan(lists, goals, max_active=3, available_minutes=10)
    assert [s.candidate.habit.id for s in by_minutes.active] == ["a", "c"]
    assert by_minutes.unresolved == {"y": "deferred"}
    by_count = template_plan(lists, goals, max_active=1, available_minutes=60)
    assert by_count.unresolved == {"y": "deferred", "z": "deferred"}


def test_later_pick_resolves_an_earlier_deferred_goal():
    big = make_habit("big", goals={"x": 1.0}, levels=(Level("standard", 9, 1),))
    both = make_habit("both", goals={"x": 0.2, "y": 1.0}, levels=(Level("standard", 3, 1),))
    goals = ["x", "y"]
    lists = {
        "x": score([candidate(big, ("x",))], goals, frozenset()),
        "y": score([candidate(both, ("x", "y"))], goals, frozenset()),
    }
    plan = template_plan(lists, goals, max_active=3, available_minutes=5)
    assert [s.candidate.habit.id for s in plan.active] == ["both"]
    assert plan.unresolved == {}


GOALS = ["a", "b", "c", "d"]
ANCHORS = ["morning", "after_school", "evening", "bedtime"]
habits = st.lists(
    st.builds(
        lambda i, goals, minutes, anchor: make_habit(
            f"h{i}", goals=dict.fromkeys(goals, 1.0), anchor=anchor, levels=(Level("standard", minutes, 1),)
        ),
        st.integers(0, 30),
        st.sets(st.sampled_from(GOALS), min_size=1),
        st.integers(1, 20),
        st.sampled_from(ANCHORS),
    ),
    unique_by=lambda h: h.id,
    max_size=12,
)


@hypothesis_settings(max_examples=200, deadline=None)
@given(
    habits,
    st.lists(st.sampled_from(GOALS), unique=True, min_size=1),
    st.integers(1, 3),
    st.integers(1, 60),
)
def test_template_plan_invariants(habit_list, goals, max_active, minutes):
    candidates = eligible(make_release(*habit_list), PROFILE, goals, available_minutes=minutes)
    lists = shortlists(score(candidates, goals, PROFILE.interests), goals)
    plan = template_plan(lists, goals, max_active=max_active, available_minutes=minutes)
    active_ids = [s.candidate.habit.id for s in plan.active]
    assert len(active_ids) == len(set(active_ids)) <= max_active
    assert sum(s.candidate.level.minutes for s in plan.active) <= minutes
    assert not any(s.candidate.needs_cue for s in plan.active)
    covered = {g for s in plan.active for g in s.candidate.goal_ids}
    assert covered.isdisjoint(plan.unresolved)
    assert covered | set(plan.unresolved) == set(goals)
    assert not set(active_ids) & {s.candidate.habit.id for s in plan.future}
    assert plan == template_plan(lists, goals, max_active=max_active, available_minutes=minutes)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_catalog_scoring.py -q --no-cov`
Expected: FAIL with `ModuleNotFoundError: No module named 'habitquest_backend.catalog.scoring'`.

- [ ] **Step 3: Write the implementation**

Create `src/habitquest_backend/catalog/scoring.py`:

```python
"""Deterministic ranking: an LLM hint, the tie-breaker and the reviewed template fallback."""

from dataclasses import dataclass

from habitquest_backend.catalog.eligibility import Candidate

RANKING_VERSION = "2026-10-29.1"
INTEREST_BONUS = 0.5
PREREQ_BONUS = 0.3
LEVEL_PENALTY = {"simple": 0.0, "standard": 0.1, "stretch": 0.3}


@dataclass(frozen=True)
class Scored:
    candidate: Candidate
    score: float
    breakdown: dict[str, float]


@dataclass(frozen=True)
class TemplatePlan:
    active: tuple[Scored, ...]
    future: tuple[Scored, ...]
    unresolved: dict[str, str]


def score(candidates: list[Candidate], goal_ids: list[str], interests: frozenset[str]) -> list[Scored]:
    """Rank candidates; goal_ids is in parent priority order (first is most important)."""
    weights = {goal: (len(goal_ids) - i) / len(goal_ids) for i, goal in enumerate(goal_ids)}
    present = {c.habit.id for c in candidates}
    result = []
    for c in candidates:
        breakdown = {
            "goal": round(sum(weights[g] * c.habit.goals[g] for g in c.goal_ids), 4),
            "interest": INTEREST_BONUS if any(f"interest:{i}" in c.habit.tags for i in interests) else 0.0,
            "prereq": PREREQ_BONUS if present & set(c.habit.supports) else 0.0,
            "level": -LEVEL_PENALTY[c.level.level],
        }
        result.append(Scored(c, round(sum(breakdown.values()), 4), breakdown))
    return sorted(result, key=lambda s: (-s.score, s.candidate.habit.id))


def shortlists(scored: list[Scored], goal_ids: list[str], size: int = 3) -> dict[str, list[Scored]]:
    return {goal: [s for s in scored if goal in s.candidate.goal_ids][:size] for goal in goal_ids}


def template_plan(
    lists: dict[str, list[Scored]], goal_ids: list[str], *, max_active: int, available_minutes: int
) -> TemplatePlan:
    """Top fitting habit per goal, within the active-habit and minutes limits; one habit counts once."""
    active: list[Scored] = []
    covered: set[str] = set()
    unresolved: dict[str, str] = {}
    minutes = 0
    for goal in goal_ids:
        if goal in covered:
            continue
        options = [s for s in lists.get(goal, []) if not s.candidate.needs_cue]
        if not lists.get(goal):
            unresolved[goal] = "no_catalog_entry"
            continue
        if not options:
            unresolved[goal] = "needs_cue"
            continue
        pick = next((s for s in options if minutes + s.candidate.level.minutes <= available_minutes), None)
        if pick is None or len(active) >= max_active:
            unresolved[goal] = "deferred"
            continue
        active.append(pick)
        minutes += pick.candidate.level.minutes
        covered.update(pick.candidate.goal_ids)
        for served in pick.candidate.goal_ids:
            unresolved.pop(served, None)
    seen = {s.candidate.habit.id for s in active}
    future = []
    for goal in goal_ids:
        for s in lists.get(goal, []):
            if s.candidate.habit.id not in seen:
                seen.add(s.candidate.habit.id)
                future.append(s)
    future.sort(key=lambda s: (-s.score, s.candidate.habit.id))
    return TemplatePlan(tuple(active), tuple(future), unresolved)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_catalog_scoring.py -q --no-cov`
Expected: `7 passed`.

- [ ] **Step 5: Lint, type-check, full suite and commit**

```powershell
uv run ruff format src tests
uv run ruff check --fix src tests
uv run python -m mypy src
uv run python -m pytest -q
```

Expected: clean; all pass; coverage 100% (every branch of `score`, `shortlists` and `template_plan` is exercised above).

```bash
git add src/habitquest_backend/catalog/scoring.py tests/test_catalog_scoring.py
git commit -m "feat(catalog): add deterministic scoring, shortlists and template plan"
```

---

### Task 6: Spec refinements, ledger and final verification

**Files:**
- Modify: `docs/superpowers/specs/2026-10-29-habit-catalog-design.md`
- Modify: `docs/implementation-ledger.md`

- [ ] **Step 1: Update the spec to match the refinements**

In `docs/superpowers/specs/2026-10-29-habit-catalog-design.md`:

1. Section 3.1: change `concerns(id text, version, status, label, synonyms text[], search tsvector)` to `concerns(id text, version, status, label, synonyms text[])`.
2. Section 3.1: change `catalog_releases(id, number, built_by, ...)` to `catalog_releases(id, built_by, ...)` and add the bullet: "`catalog_releases.id` is the release number."
3. Section 3.1: change `habit_variants(habit_id, version, interest, title, instruction, status)` to `habit_variants(habit_id, version, interest, title, instruction)` and add the bullet: "Variants belong to a habit version and are reviewed with it."
4. Section 4.2 step 2: change "**Eligibility (SQL).**" to "**Eligibility (deterministic, Python over the loaded release; service roles can read only published, approved rows).**" and replace "accessibility tags match the child's needs" with "every accessibility need `x` of the child appears on the habit as `access:x`".
5. Section 5.5: change the first row's path to `/v1/editor/{habits,goals,concerns,chapters}`.
6. Sections 4.3 and 7: replace the reasons `age`, `needs_cue` or `no_catalog_entry` with `no_catalog_entry`, `needs_cue` or `deferred` (`deferred` = blocked by `max_active_habits` or minutes).
7. Section 6 step 2: append "Release 1 is seeded idempotently by `supabase/seed.sql`."

- [ ] **Step 2: Add a ledger entry**

Append to `docs/implementation-ledger.md`:

```markdown
## Catalog foundation (plan 1 of 3)

- Added versioned catalog tables, approval immutability triggers, two-person review checks, numbered releases and read-only `released_*` views; service roles cannot read catalog tables directly.
- Seeded release 1 from the development catalog (five habits, five goals, four concerns, fifteen chapter templates).
- Added `habitquest_backend.catalog`: release loading, deterministic eligibility, scoring (`RANKING_VERSION 2026-10-29.1`), per-goal shortlists and template plan, with Hypothesis invariants.
- Planning still uses `hq.catalog`; plan 2 switches it and removes the old tables.
```

- [ ] **Step 3: Full verification**

```powershell
uv run ruff format --check src tests scripts
uv run ruff check src tests scripts
uv run python -m mypy src
uv run python -m pytest --cov=habitquest_backend --cov-report=term-missing
```

Expected: no format, lint or type errors; all tests pass (79 existing plus the new catalog tests); `Required test coverage of 100% reached`.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-10-29-habit-catalog-design.md docs/implementation-ledger.md
git commit -m "docs: record catalog foundation refinements and ledger entry"
```
