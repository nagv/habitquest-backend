# Execution ledger - local onboarding backend

Plan: HabitQuest AI Functional Workflow v6, section 15, and the implementation steps in this chat.

Scope: authenticated concerns -> confirmed goals -> reviewed catalog-backed habits and chapter outline -> versioned parent-confirmed plan. Rewards and live world execution remain later integrations.

Pre-flight: API consumes typed scope/contracts; worker consumes durable job and checkpoint IDs; confirmation consumes reviewed proposal plus current source/plan versions. These interfaces must agree throughout.

Ruling: Work in the user-requested Backend folder, preserving the existing empty HabitQuestAgent folder. No existing Git repository or application files were present. No additional worktree is necessary for this new repository.

Ruling: Docker and Ollama are unavailable on this host. Verify mechanics with an isolated native PostgreSQL 18 cluster and deterministic model; also provide local Supabase configuration, real JWT/JWKS validation, and Ollama integration. Real Supabase Auth and real model smoke tests cannot be claimed without those runtimes.

Ruling: Stage files in the chat's writable working directory, then copy only project-owned files to the authorized destination using elevated filesystem access. Do not copy the staging virtual environment.

## Evidence

- Initialized Python 3.12 uv package and installed locked agent/API/database/test dependencies.
- First contract tests written before application implementation.

## Final verification

- Independent read-only review identified checkpoint fencing, source-lock races, full source-field validation, session/plan locking, final-candidate review, durable deadlines/budgets and membership insertion scope. Fixed with regression tests; no critical/important finding was left open.
- Target-folder run: 79 tests passed; 512 owned application statements and 116 branch opportunities, all covered (100%). Worker subprocess measurement included.
- Ruff check/format and `python -m mypy src --follow-imports=silent` passed. Windows blocked the mypy executable launcher; its standard Python module entrypoint passed.
- Native PostgreSQL cluster bootstrapped under .local with SCRAM password authentication and loopback binding; startup/restart and API readiness verified.
- Actual API plus separate worker-process synthetic demo produced confirmed bedtime, morning and responsibility habits and three Space chapters.
- npm audit: zero findings after updating the development Supabase CLI to 2.122.0.
- Supabase CLI launch returned spawn UNKNOWN; Docker and Ollama are absent. Supabase runtime/Auth, pgTAP and downloaded-model smoke checks remain unverified. Native tests do not establish model recommendation quality.
- Ruling: reserved_tokens records conservative request reservations, not measured tokenizer usage/cost; this distinction is documented. Persisted call/deadline limits remain enforced.
- Ruling: explicit failure is the first-slice fallback. No unchecked generated plan is published when review, suitability or budgets fail. A richer approved-template fallback can follow.
- Ruling: no hosted release, reward ledger or real family pilot is included. Family deletion/retention, evals and production credentials remain prerequisites for a family pilot.
- Ruling: one coherent initial feature commit is used for this new repository; no pre-existing application history was available. No remote push or deployment.
- Deferred minor items: none from the independent review. Broader product capabilities remain intentionally out of scope as specified in implementation-plan.md.

## Catalog foundation (plan 1 of 3)

- Added versioned catalog tables, approval immutability triggers, two-person review checks, numbered releases and read-only `released_*` views; service roles cannot read catalog tables directly.
- Review follow-up (migration `202610290002_catalog_guards.sql`): published releases may change only `is_current`/`notes` and cannot be deleted; withdrawal is permanent; detail guards lock the parent row `FOR SHARE` to close approve/publish races.
- Seeded release 1 from the development catalog (five habits, five goals, four concerns, fifteen chapter templates). Plan seed bug fixed during implementation (`hq.habits.id`, not `habit_id`).
- Added `habitquest_backend.catalog`: release loading, deterministic eligibility, scoring (`RANKING_VERSION 2026-10-29.1`), per-goal shortlists and template plan, with Hypothesis invariants.
- Planning still uses `hq.catalog`; plan 2 switches it and removes the old tables. Open for plan 2/3: publish-time release validation (dangling goal/entity references, habits without levels) and clearing the previous current release before setting a new one.
- Final review follow-up: `score` and `eligible` de-duplicate goal ids and tolerate candidate goals outside the priority list. Carried to plan 2: a tested `(habit_id, version)` revalidation helper for confirmation (not in release / superseded / withdrawn); `load_release` should reject unpublished or unknown release ids instead of returning an empty release; distinguish "already covered by an active habit" from `no_catalog_entry`; compute `needs_cue` over all candidates rather than the top-3 shortlist; LLM-facing shortlist serialisation and code-enforced pick validation; read the release in one snapshot.

## Intake and catalog-backed planning (plan 2a)

- Child profile (interests, accessibility, opted-in topics, preferred world, routine schedule, household timezone) with versioned updates; repeatable intakes replace sessions and goal confirmation; preferences migrated into the profile; old catalog tables and analysis jobs retired.
- Planning job: LLM mapping to release goal IDs, deterministic eligibility/scoring/template, Planner tool scoped to server-built shortlists, code-enforced pick validation, mandatory review, template fallback.
- Confirmation locks and revalidates active habits and chapters against the current release (`replan_required`), writes `release_id`, and records active goals in `child_goals`.
- Implementation deviations: the household upsert in `POST /v1/children` became insert, membership, then update, because an upsert's new row must pass the household SELECT policy before the membership exists; fixture TRUNCATE of catalog tables uses CASCADE now that proposals and plans reference releases.
- Not run on this host: pgTAP `supabase/tests/rls.sql` (needs the Supabase stack) and `scripts/demo.py` (needs a running API). Intermittent timing-test failures were observed under heavy machine load; each passed when rerun alone.
- Review fixes: strict goal coverage (`covered_goals`) in pick validation and the proposal, with per-habit `covers`; template prefers dedicated habits and defers weakly served goals; Planner shortlists of cue-ready options plus template picks; mapper quotes must be substrings of the concerns; carried habits revalidated (drop/upgrade changes, `added_in_plan_version`); `available_minutes` is the daily total including carried habits; `profile_version` checked at publication and confirmation; the checked release id is recorded on proposals and plans; `child_goals` reactivated when raised again; legacy plans (`release_id IS NULL`) carry nothing; cap rationale when carried habits fill the limit.
- Known, not fixed: a non-owner household member's timezone handling can return 500 (no multi-member households exist yet). When the remaining daily minutes are 0, goals report `no_catalog_entry` rather than `deferred` because nothing is eligible.
- Remaining for plan 2b: proposal edits, replace/rotate/pause change sets, plan flags, catalog requests, concern dataset.

## Editorial API (plan 3)

- Migration `202610290006_editorial.sql`: `hq_editor` role, `catalog_audit`, editor-only `*_version_details` and `release_*` views (any release, published or not), column-level grants so editors cannot set status, reviewer, withdrawal or author columns (`created_by` defaults to `hq.my_editor()`), and a guard freezing versions and their details while `in_review`.
- `SECURITY DEFINER` functions `transition_version` (submit/approve/reject with audit; reviewer differs from author), `build_release` (latest approved, non-withdrawn versions, or a rollback copy minus withdrawn habit versions), `publish_release` (advisory lock, publisher differs from builder, switches `is_current`) and `withdraw_habit` (permanent, audited, flags each child's current plan that uses the habit with a fixed parent-facing reason; the editor's reason stays internal).
- API: `Editor` dependency (`app_metadata.hq_role == "editor"`, otherwise 403), `/v1/editor/*` routes in `editorial/api.py`; function refusals map to 403/404/409/422. Submit checks and the lexical lint live in `editorial/checks.py`; release validation and diff in `catalog/releases.py`.
- Replaces the plan 2b Python `withdraw_habit` helper, which ran with household privileges; withdrawal now happens only through the editor endpoint and database function.
- Decisions: rejection returns the version to `draft` (audited with the reason) rather than leaving it `rejected`; one open draft per entity; linked goals must exist in any status at submit, and the release validation catches references outside the release; concern labels are parent descriptions of problems and are not linted.
- Known limits: release validation runs in the API before `publish_release`, so a direct database caller with the `hq_editor` role could publish an invalid release; the optional LLM lint is not implemented; parent notification of plan flags is a later slice; pgTAP additions in `supabase/tests/rls.sql` were not run on this host.

