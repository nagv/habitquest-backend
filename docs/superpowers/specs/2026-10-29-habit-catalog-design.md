# Habit catalog, concern intake and editorial design

Date: 2026-10-29
Status: approved design, pending implementation plan
Baseline: HabitQuest AI Functional Workflow v6 (sections 5, 6, 9, 14.1-14.6, 15) and the onboarding backend at commit `ca7e45f`.

## 1. Goal

Parents describe pain points for a child. The backend suggests suitable, reviewed habits from a versioned catalog, organised under positive goals, for the parent to review and confirm. Parents can add new pain points for the same child later; each round produces a change set against the child's current plan. Staff editors maintain the catalog through a two-person review and release process.

## 2. Decisions

| Topic | Decision |
|---|---|
| Scope | Catalog data model, concern-to-habit suggestion, profile/intake split and editorial API. |
| Storage | Relational catalog with immutable versions and numbered releases (not JSONB documents or repository YAML). |
| Sex | Not collected. Sensitive topics (for example puberty hygiene, ages 10-13) are catalog topic tags that a parent explicitly opts into. |
| Interests | Never exclude a habit. They raise ranking and select reviewed interest-themed wording variants. |
| LLM role | Maps concerns to goal IDs and picks habits from the deterministically eligible set. It never creates parent- or child-facing activities. |
| Ranking | Deterministic score is computed for every eligible habit; it is a hint to the LLM, the tie-breaker and the template fallback. |
| Goal step | Goals remain catalog data and group habits, but there is no separate goal-confirmation step. One planning job, one parent review screen. |
| Profile vs intake | Child profile holds durable facts and the routine schedule. Concern intake is a repeatable round that produces a change-set proposal. |
| Editors | Staff with an admin-assigned `app_metadata.hq_role = "editor"` claim. Reviewer differs from author; publisher differs from release builder. |

This changes v6 section 14.1: steps 3 (confirm positive goals) and 6 (review and confirm) merge into one parent review. Record this in the v6 decision register.

## 3. Data model

All tables live in schema `hq`.

### 3.1 Catalog

```
concerns(id text, version, status, label, synonyms text[])
goals(id text, version, status, title, description)
concern_goals(concern_id, concern_version, goal_id, weight numeric)

habits(id text PRIMARY KEY)                      -- stable canonical activity identity
habit_versions(habit_id, version, status, title, instruction, min_age, max_age,
               routine_anchor, safety_notes, created_by, reviewed_by, reviewed_at,
               withdrawn_at, withdrawn_reason)
habit_levels(habit_id, version, level, minutes, points, instruction)
habit_goals(habit_id, version, goal_id, strength numeric)
habit_prereqs(habit_id, version, supports_habit_id, kind)
habit_tags(habit_id, version, tag)               -- interest:*, topic:*, access:*
habit_variants(habit_id, version, interest, title, instruction)
chapter_templates(id, version, status, world, goal_id, title)

catalog_releases(id, built_by, published_by, published_at, notes, is_current)
release_items(release_id, entity, entity_id, version)
catalog_audit(entity, entity_id, version, from_status, to_status, actor, reason, at)
catalog_requests(id, phrase_hash, normalized_phrase, count, first_seen, last_seen)
```

- `status` for versioned entities: `draft | in_review | approved | rejected`.
- `catalog_releases.id` is the release number. Published releases may change only `is_current` and `notes`.
- Variants belong to a habit version and are reviewed with it.
- Withdrawal of an approved habit version is permanent.
- `routine_anchor`: `morning | after_school | evening | bedtime`.
- `level`: `simple | standard | stretch`; one to three levels per habit version.
- Constraints: `min_age <= max_age`, ages within 6-13, `points BETWEEN 1 AND 3`, `minutes > 0`, unique `(release_id, entity, entity_id)`, at most one `is_current` release (partial unique index).
- A trigger rejects `UPDATE` of any approved version, except the withdrawal fields.
- `catalog_requests` stores normalized unmatched phrases only when the parent consented; no child or household identifiers.

### 3.2 Child profile and intake

```
children: + interests text[], accessibility text[], opted_in_topics text[],
            preferred_world, routine_schedule jsonb, profile_version int
            -- routine_schedule: {anchor: {cue: "HH:MM", days: ["mon",...]}}
intakes(id, child_id, parent_id, version, concerns text, available_minutes,
        base_plan_version, created_at)
child_goals(child_id, goal_id, status, since_plan_version, source_intake_id)
            -- status: active | paused | resolved
plan_flags(id, plan_id, kind, habit_id, reason, created_at, resolved_at)
```

Interest, accessibility and topic values come from fixed vocabularies validated in the API schemas. `child.name` remains a parent-chosen nickname.

### 3.3 Roles and access

- `hq_api` and `hq_worker`: `SELECT` only on views exposing the current release (and the specific versions referenced by plans and proposals). No access to drafts.
- `hq_editor`: writes draft rows; no access to household tables.
- `SECURITY DEFINER` functions perform `approve`, `reject`, `publish_release` and `withdraw_habit`, enforcing the two-person rules and writing `catalog_audit`.

## 4. Concern intake and suggestion flow

### 4.1 API

| Endpoint | Purpose |
|---|---|
| `POST /v1/children`, `PATCH /v1/children/{id}` | Create or edit profile, including `routine_schedule`; `PATCH` requires `expected_profile_version`. |
| `POST /v1/children/{id}/intakes` | Save concerns and `available_minutes`; returns intake and missing-cue questions. |
| `POST /v1/intakes/{id}/plan-runs` | `Idempotency-Key` header; queues one planning job; returns 202, run ID, status URL. |
| `GET /v1/plan-runs/{id}` | Job status. |
| `GET /v1/proposals/{id}` | Review-screen payload. |
| `PATCH /v1/proposals/{id}` | Deterministic parent edits; requires `expected_proposal_version`. |
| `POST /v1/proposals/{id}/confirm` | `expected_proposal_version` and `expected_plan_version`; idempotent. |
| `GET /v1/children/{id}/plan`, `GET /v1/children/{id}/goals` | Current confirmed plan; goal statuses with source intakes. |

Removed: `/onboarding`, `/clarifications`, `/confirm-goals`, `/analyze-runs`, the `analysis` job kind, `sessions.goals_confirmed`, and the `Category` literal.

### 4.2 Planning job steps

1. **Map concerns (LLM, Director).** Input: intake concerns, active `child_goals`, and the goal and concern IDs of the current release. Output: `{goals: [{goal_id, evidence_span, confidence, suggested_not_stated}], unmatched_phrases, explanation}`. IDs outside the release fail validation. Existing active goals are reused rather than duplicated.
2. **Eligibility (deterministic, Python over the loaded release; service roles can read only published, approved rows).** Habit version is in the current release and not withdrawn; the child's whole age band lies within `[min_age, max_age]`; it serves at least one mapped goal; topic tags are in `opted_in_topics`; every accessibility need `x` of the child appears on the habit as `access:x`; it does not duplicate an active canonical habit in the current plan; at least one level fits `available_minutes`. A habit whose `routine_anchor` has no cue in `routine_schedule` is returned with `needs_cue`.
3. **Score (Python, `ranking_version`).**
   `score = Σ(goal_priority_weight × habit_goals.strength) + interest_bonus + prereq_bonus − level_penalty`; ties broken by habit ID. Goal priority is the parent's order once set; before that, existing active goals first, then mapped goals by descending confidence, then by position of `evidence_span` in the concerns text. Produces a per-goal shortlist (up to three) with score breakdowns, and a template plan: top habit per goal, simplest fitting level, within `max_active_habits` and total `available_minutes`.
4. **Pick (LLM, Planner).** Tool `search_catalog(goal_id)` returns that goal's shortlist; scope is bound server-side. Output: change set (`add`, `replace`, `rotate_to_future`, `pause_goal`) with level, cue and days per active habit, future candidates, rationale, order explanation and routine links.
5. **Validate (Python).** All IDs and levels are on the shortlist; every active goal is covered through `habit_goals`; active habits ≤ `max_active_habits` after applying the change set; total minutes ≤ `available_minutes`; a habit serving several goals counts once.
6. **Review (LLM, Reviewer, mandatory).** One revision on rejection. If validation or review still fails, or the budget is exhausted, publish the template plan with `fallback: true` and a reviewed template rationale.

Child-facing title and instruction come from an interest-matching approved `habit_variant` when one exists, otherwise the habit version. Canonical ID, level, minutes and points are fixed fields.

### 4.3 Proposal contents

`catalog_release_id`, `ranking_version`, model and prompt versions, base plan version, proposal version, mapped goals with evidence, change set, active habits with level/cue/days, future candidates, shortlists with scores, routine links, chapter outline (world × goal templates), unresolved goals with reasons, questions, rationale, order explanation, review result, `fallback`, expiry.

### 4.4 Parent review edits

`PATCH` operations: remove goal, reorder goal priority, swap habit within its goal's shortlist, change level, cue or days, promote a future candidate, demote an active habit. Each edit re-runs step 5, increments `proposal.version` and records the actor. Adding a goal returns `409 replan_required`; the client starts a new plan run with that goal pinned.

### 4.5 Confirmation

Existing locking and source revalidation, extended: the release and every referenced version must be unchanged in the current release and not withdrawn, otherwise `409 replan_required`. Confirmation writes an immutable plan version, updates `child_goals`, and makes any other open proposal for the child stale through `expected_plan_version`. Profile edits never modify a confirmed plan; an edit that invalidates one (for example removing a cue an active habit uses) creates a `plan_flags` row.

## 5. Editorial workflow

### 5.1 Identity

A new `Editor` dependency verifies the JWT as today and requires `app_metadata.hq_role == "editor"`. Editor routes live under `/v1/editor/*`. A request acts in exactly one role.

### 5.2 Lifecycle

```
draft --submit--> in_review --approve (different editor)--> approved (immutable)
  ^                   |
  +---reject+reason---+        editing an approved entity creates a new draft version
```

Submit runs deterministic checks: schema, age bounds, one to three levels with valid minutes and points, existing linked goals, and a lexical lint for v6 section 9 prohibited content (punishment, secrecy, dangerous activity, weight loss, diagnosis or treatment, coercive wording) returning reason codes. An optional LLM lint may add findings but never clears a draft.

### 5.3 Releases

- An editor builds a release candidate from approved versions; `GET /v1/editor/releases/{id}/diff` compares it with the current release.
- `validate` errors: missing chapter template for any world × goal; `habit_goals` or `habit_prereqs` referencing entities outside the release; a concern with no goal. Warnings: a goal with no eligible habit for an age band.
- `publish` requires a different editor from `built_by`, takes an advisory lock, and switches `is_current`.
- Rollback republishes a prior release's items as a new release number.

### 5.4 Withdrawal

`POST /v1/editor/habits/{id}/withdraw` with a reason sets `withdrawn_at` immediately. Open proposals using the habit cannot be confirmed; confirmed plans containing it receive a `plan_flags` row for parent review. Parent notification is a later slice.

### 5.5 Endpoints

| Path | Actions |
|---|---|
| `/v1/editor/{habits,goals,concerns,chapters}` | create draft, edit draft, `submit`, `approve`, `reject` |
| `/v1/editor/releases` | create, `diff`, `validate`, `publish` |
| `/v1/editor/habits/{id}/withdraw` | emergency withdrawal |
| `/v1/editor/catalog-requests` | anonymized unmatched concerns |

## 6. Migration

Forward-only migrations:

1. Create catalog, release, audit, intake, `child_goals` and `plan_flags` tables; add profile columns to `children`.
2. Seed release 1 from the existing `hq.catalog` and `hq.chapters`, preserving the five habit IDs. Goals: the four existing categories plus "Predictable bedtime and mornings". Concerns with synonyms. One `standard` level per habit using current minutes and points. Routine anchors: `bedtime-prep` → `bedtime`, `pack-bag` → `evening`, `morning-ready` → `morning`, `join-meal` and `clear-plate` → `after_school`. `clear-plate` links to both meals and responsibility. Release 1 is seeded idempotently by `supabase/seed.sql`.
3. Convert `hq.sessions` to `intakes`; copy the latest `preferences` world and routine times into profile fields; set `catalog_release_id = 1` on existing plans. Existing `analysis` jobs stay readable.
4. Drop `hq.catalog`, `hq.chapters`, `sessions.goals_confirmed`, and the `Category` literal in `schemas.py`.

Restructure code into the v6 15.2 layout as part of this work: `catalog/` (repository, eligibility, scoring, releases), `editorial/` (API, lifecycle), `intake/` (profile, intakes), `planning/` (agents, validation, template fallback). Move the scripted fake model from `src` into test support.

## 7. Error handling

| Case | Behaviour |
|---|---|
| Unknown goal or habit ID, or off-shortlist pick | Validation failure, one revision, then template plan with `fallback: true`. |
| Goal with no eligible habit | Listed in `unresolved` with reason `no_catalog_entry`, `needs_cue` or `deferred` (blocked by `max_active_habits` or minutes); run succeeds for other goals. |
| All goals unresolved | Run `ready` with no active habits and questions; not a failure. |
| Release changed or habit withdrawn before confirm | `409 replan_required`. |
| `PATCH` violating limits or coverage | `422` naming the violated rule. |
| Stale proposal, plan or profile version | `409`. |
| Author approves own draft, builder publishes own release, or non-editor | `403` from the API; the database function also refuses. |

## 8. Testing

The 100% owned statement and branch coverage gate remains.

- **Unit and property (Hypothesis):** scoring determinism and ordering; eligibility combinations (age band edges, topics, accessibility, `needs_cue`); change sets never exceed `max_active_habits`; multi-goal habits count once.
- **Database (pgTAP, run in CI):** approved immutability; two-person approval and publication; single current release; `hq_api` and `hq_worker` cannot read drafts; `hq_editor` cannot read household tables.
- **Agent contract (scripted fake model through the real graph):** invented ID leads to revision then fallback; off-shortlist pick rejected; the v6 14.2 Alex example end to end (goals with evidence, meals goal marked `suggested_not_stated`, three active habits and two future candidates); a second intake producing a replace or rotate change set.
- **API:** `PATCH` edits and version increments; add-goal returns `replan_required`; release published mid-proposal; withdrawal flags confirmed plans; editor route authorization.
- **Labelled concern dataset:** about 50 cases in `tests/data/concerns.jsonl`. Runs against the fake model as a regression check; an opt-in `live_local` Ollama run reports goal and habit recall without failing the build.

## 9. Out of scope

Daily occurrences, rewards and Spark ledger, parent notifications, AI-drafted catalog entries, pgvector or embedding retrieval, multi-caregiver households.

## 10. Implementation refinements (plan 2a)

- The scripted fake model remains in `src` because `MODEL_MODE=fake` is a supported runtime mode.
- Intake creation returns the intake; cue and clarification questions arrive with the proposal.
- Confirmation revalidates active habits and chapters; future candidates are revalidated when promoted.
- Budget, deadline, lease and cancellation failures fail the run. Invalid selections after one revision and rejected reviews publish the template with `fallback: true`. A goal mapping outside the release fails the run.
- Template coverage: a habit satisfies the goal it was picked for and goals it serves with strength ≥ 0.8; a weaker link satisfies a goal only when no other unpicked option exists.
- Second intakes carry the current plan's active habits forward and add habits. Carried habits are revalidated against the current release and profile: dropped (`not_in_release`, `withdrawn`, `level_removed`, `needs_cue`) or upgraded to the current version, each recorded in `changes`. `available_minutes` is the child's daily total including carried habits, and `max_active_habits` counts carried habits; when carried habits fill the limit the proposal says new goals are listed for later. Replace, rotate and pause come in plan 2b.
- Strict coverage in validation and the proposal: a chosen habit covers a goal it serves with strength ≥ 0.8, or a goal it serves at all once every cue-ready option for that goal is chosen. Each active entry records the goals it `covers`. The template prefers a dedicated habit for each goal and defers a goal left only weakly served while a dedicated option remains.
- The Planner's shortlist per goal is the top cue-ready options plus any template pick for that goal.
- Mapper `evidence_span` values and unmatched phrases must be non-empty, case-insensitive substrings of the concerns; otherwise the run fails.
- Proposals record the child's `profile_version`; publication and confirmation reject a proposal whose profile changed. Publication and confirmation record the id of the release they actually checked (`proposals.release_id`, `plans.release_id`).
- The household timezone is set through the child profile endpoints. Legacy plans keep `release_id = NULL` and carry nothing forward.

## 11. Implementation refinements (plan 3)

- Rejection returns a version to `draft` with an audited reason; the `rejected` status is not used.
- Editors write drafts directly through column-level grants; `created_by` is always the calling editor. Status changes, releases, publication and withdrawal go through `SECURITY DEFINER` functions. Versions and their details are frozen while `in_review`.
- Endpoints: `/v1/editor/{kind}/{id}/versions[/{n}[/submit|/approve|/reject]]`, `/v1/editor/releases[/{id}/diff|/validate|/publish]`, `/v1/editor/habits/{id}/withdraw`, `/v1/editor/catalog-requests`.
- Release builds take the latest approved, non-withdrawn version of each entity; rollback copies a published release's items without withdrawn habit versions. Validation also reports withdrawn habits and habits without levels.
- Withdrawal flags only each child's current plan, with a fixed parent-facing reason.

## 12. Implementation refinements (local flow fixes)

- Parent edits rebuild entries from the current release and profile and re-run limits, cue availability and strict coverage; `422` details start with the violated rule's name. Removing coverage of a goal requires `remove_goal` first.
- Eligibility uses the child's daily `available_minutes`; habits that fit the day but not the remaining time are `deferred` and remain future candidates.

