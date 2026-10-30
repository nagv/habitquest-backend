---
type: concept
title: "Tests, coverage gate, pgTAP and the demo harness"
description: "How correctness is verified: the disposable database guard and fixtures, the fake-by-default model, the 100% branch coverage floor, what each test group defends, the pgTAP RLS test, and the scripted demo."
tags: [testing, pytest, coverage, pgtap, fixtures, ci, security, isolation]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-30T00:13:47.181Z
sources:
  - id: openwiki-source-4f2678f93d3fd3835f9f2909
    resource: repo://.github/workflows/test.yml
  - id: openwiki-source-05ccef8d4cf1698187f20464
    resource: repo://pyproject.toml
  - id: openwiki-source-d7ed0bb14d48547c9f500bad
    resource: repo://scripts/demo.py
  - id: openwiki-source-739330f1287a2173c961b478
    resource: repo://scripts/smoke_supabase_auth.py
  - id: openwiki-source-49807a32b3bacd836c1dabb0
    resource: repo://supabase/tests/rls.sql
  - id: openwiki-source-f0a6e7dc03522b2682f88655
    resource: repo://tests/conftest.py
  - id: openwiki-source-4cd8e518e87603618edcf1e6
    resource: repo://tests/test_catalog_eligibility.py
  - id: openwiki-source-96fe3d1a005f8ccbcd97437c
    resource: repo://tests/test_concern_dataset.py
  - id: openwiki-source-76a6ea1060b2d4b20b577a89
    resource: repo://tests/test_database_flow.py
  - id: openwiki-source-7ea43d980732d932f3c11888
    resource: repo://tests/test_editorial.py
  - id: openwiki-source-3f7eb9cc39706a9c4fe29584
    resource: repo://tests/test_final_boundaries.py
  - id: openwiki-source-481ae2004eda0e7029d60d90
    resource: repo://tests/test_goal_coverage.py
  - id: openwiki-source-6300e00bb954ddbbea3f1c0d
    resource: repo://tests/test_hosting.py
  - id: openwiki-source-441b70bbfa338b2de376ac14
    resource: repo://tests/test_local_scripts.py
  - id: openwiki-source-f423df38948f468a55393ae5
    resource: repo://tests/test_onboarding.py
  - id: openwiki-source-84f0d7d2922a389a7a007cc2
    resource: repo://tests/test_planning_selection.py
  - id: openwiki-source-1002a26485deb3004fcb06d5
    resource: repo://tests/test_proposal_patch.py
  - id: openwiki-source-4c573540c2631e3dec22664a
    resource: repo://tests/test_recovery.py
  - id: openwiki-source-8b9a981f994439cf343d35ae
    resource: repo://tests/test_releases_and_checks.py
  - id: openwiki-source-39f04a503f63bbc178ac6d3e
    resource: repo://tests/test_runtime_edges.py
  - id: openwiki-source-d2d0c99e56c7698f3b5df7e2
    resource: repo://tests/test_security_and_changes.py
generated: { by: "openwiki/0.7.2", at: "2026-10-30T00:13:47.181Z" }
---

# Tests, coverage gate, pgTAP and the demo harness

Every check in this repository is automated in CI (`backend-checks`). The suite runs against a disposable PostgreSQL container, uses a fake LLM by default, and enforces three hard gates: **lint**, **types**, and **100% statement and branch coverage** of `habitquest_backend`. This page explains the guardrails, what each test group defends, and which checks require external services.

## Disposing the test database

<!-- openwiki: broken internal link [../tests/conftest.py] file "../tests/conftest.py" does not exist. Fix the href or restore the target, then delete this comment. -->
The [`conftest.py`](../tests/conftest.py) fixture module enforces the rule before running any test:

<!-- openwiki: broken internal link [../tests/conftest.py#L20-L24] file "../tests/conftest.py" does not exist. Fix the href or restore the target, then delete this comment. -->
[`conftest.py#L20-L24`](../tests/conftest.py#L20-L24)
```python
if not conninfo_to_dict(URL).get("dbname", "").endswith("_test"):
    raise pytest.UsageError(
        "TEST_DATABASE_URL must name a dedicated database ending in _test; fixtures delete test data."
    )
```

<!-- openwiki: broken internal link [../tests/conftest.py#L55-L69] file "../tests/conftest.py" does not exist. Fix the href or restore the target, then delete this comment. -->
This is not cosmetic; `conftest.py` **deletes all test data after every test** by truncating every application and editorial table [`tests/conftest.py#L55-L69`](../tests/conftest.py#L55-L69), then re-seeds the catalog fixture from `supabase/seed.sql`. The tests must therefore point at a database whose name ends in `_test`. Engineers must not override `TEST_DATABASE_URL` to anything without that suffix — pointing the suite at a household database will destroy household data.

## Fake model by default

<!-- openwiki: broken internal link [../tests/conftest.py#L33-L35] file "../tests/conftest.py" does not exist. Fix the href or restore the target, then delete this comment. -->
<!-- openwiki: broken internal link [#labelled-dataset-concern-to-goal-mapping] heading anchor "labelled-dataset-concern-to-goal-mapping" does not exist in /openwiki/testing/verification-strategy.md. Fix the href or restore the target, then delete this comment. -->
The test `settings` fixture hard-codes `model_mode="fake"` — a deterministic, purely local planner/reviewer round-trip that requires no network or GPU [`conftest.py#L33-L35`](../tests/conftest.py#L33-L35). This makes the planning and recovery tests fast and hermetic, but it is a **functional test of orchestration**, not a quality claim about model output. The `fake` mode does not exercise prompt wording, latency, or the true distribution of hallucinations. These are covered separately by the concern-dataset recall test (see [Labelled dataset: concern to goal mapping](#labelled-dataset-concern-to-goal-mapping)) and by the scripted demo.

## CI gates

`.github/workflows/test.yml` runs every push and pull request in this order:

1. `scripts/setup_db.py` — create tables in the postgres service.
2. `ruff check src tests scripts`
3. `ruff format --check src tests scripts`
4. `mypy src`
5. `pytest --cov=habitquest_backend --cov-report=term-missing --cov-report=xml --cov-report=html`

`pyproject.toml` encodes `fail_under = 100` for branch coverage in `tool.coverage.report` and `branch = true` in `tool.coverage.run`, so every push is rejected if more than zero uncovered owned statements or branches exist. The report also runs with `pytest-socket` installed; network-safety is structural, not optional.

### Coverage caveat

Reaching 100% of owned `habitquest_backend` statements and branches is a **gate**, not a proof of scenario exhaustiveness. A branch can be executed without exposing a latent integration bug. The coverage floor prevents dead code from being merged; it does not replace integration testing.

## Script checks that need external services

Some checks cannot run in CI because they require Docker or a downloaded model:

| Check | Why it needs a special environment |
|---|---|
| `scripts/smoke_supabase_auth.py` | Calls `npx supabase status`, hits the local Supabase Auth endpoint, and verifies `verify_token` with a real JWT. Requires the Supabase CLI and local services. |
| `pytest -m live_local` (concern dataset) | Runs the same concern-to-goal cases through a downloaded Ollama model. The marker is defined in `pyproject.toml` markers and never runs in CI. |
| `scripts/demo.py` (live mode) | Exercises a deployed API against a real worker process or remote Render instance. |

All three are opt-in and fail the build only if someone runs them locally without the prerequisite service.

## Test groups grouped by invariant

The tests are not listed file by file; they are grouped below by the user-visible or architectural invariant they prove.

### Isolation and authentication

**Files:** `test_security_and_changes.py`, `test_database_flow.py`, `test_onboarding.py`, `test_local_scripts.py`

These settle the boundary of what one authenticated caller can see or change:

- **JWT claim validation** (`test_security_and_changes.py`): `verify_token` rejects expired, malformed, missing-sub, wrong-audience and wrong-issuer tokens, and tokens with `role` other than `authenticated`. It also validates a real RS256 signature via a JWKS path when `jwt_secret` is absent.
- **Household isolation** (`test_database_flow.py`, `test_security_and_changes.py`): A parent token resolves to exactly one household. Every route that reads child, intake, plan, proposal, or flag data returns 404 when called with a token for a different household, even if the UUID exists. Duplicate job creation is idempotent by `Idempotency-Key`.
- **Readiness** (`test_onboarding.py`): `/health/ready` returns 503 when the database is unreachable; onboarding routes return 401 without an Authorization header.
- **Local token shape** (`test_local_scripts.py`): The parent/editor tokens generated by `local_auth.py` verify under the same `verify_token` used in production, but only when `LOCAL_DEMO_AUTH` is set.

### Leases and recovery

**Files:** `test_recovery.py`, `test_runtime_edges.py`

The worker is a stateless claim-loop over `hq.jobs`. These tests prove it is safe to crash:

- **Expired-lease reclamation** (`test_recovery.py`): A job with an expired lease can be claimed by a new worker instance; the old token is fenced out.
- **Cancellation during work** (`test_recovery.py`): Cancelling a run while `generate` is in-flight fences the model and prevents publication.
- **State changes during generation** (`test_recovery.py`): If the intake or proposal source row changes version while the model is running, the worker fails the job rather than publish stale data.
- **Provider failure retries** (`test_recovery.py`): Transient connection errors trigger exactly three attempts, then fail.
- **Checkpoint recovery** (`test_runtime_edges.py`): Crashing before LangGraph publishes a durable checkpoint leaves the job in `queued`; the next worker run resumes from the checkpoint and finishes.
- **CLI worker process** (`test_runtime_edges.py`): `python -m habitquest_backend.worker --once` runs the same code path as the in-process worker.

### Planning and coverage

**Files:** `test_planning_selection.py`, `test_goal_coverage.py`, `test_concern_dataset.py`

These prove the planner and catalog selection logic is consistent and safe:

- **Eligibility** (`test_catalog_eligibility.py`): Withdrawn, age-mismatched, active and un-cued habits are excluded. Topic tags require parent opt-in; accessibility tags (e.g. `short_instructions`) are enforced. Correct level is chosen by available minutes.
- **Selection validation** (`test_planning_selection.py`): Invalid picks (off-shortlist, duplicate, wrong level, missing cue, drops a goal, exceeds time or max-active) are rejected before they reach the parent. Weak links that would drop a dedicated habit are caught.
- **Goal coverage** (`test_goal_coverage.py`): If the planner drops a coverable goal, the final proposal is flagged with `fallback=True` and the reviewed template is used instead.
- **Labelled dataset: concern to goal mapping** (`test_concern_dataset.py`): 49 hand-labelled parent concerns are run through `fake_mapping` to assert at least the stated recall threshold per goal. Cases with `min_recall=0` prove that unmatched phrases surface questions rather than inventing habits. Setting `LIVE_LOCAL=1` runs the same cases against a local Ollama model as an informational report (not a gate).

### Proposal edits

**File:** `test_proposal_patch.py`

Parents can patch a proposal before confirming. This group proves the edit rules:

- `change_schedule` moves a cue and days; `change_level` refuses levels the habit does not define.
- `promote_future` requires the child’s profile to define the needed cue and the plan not to be full.
- `demote_active` requires the remaining active habits still cover all goals; `remove_goal` is a valid way to first relax that requirement.
- Every edit increments the proposal version; edits after a profile change, after the proposal expires, or with a stale expected version return 409.
- Promoting a habit that has been withdrawn since the plan was generated returns 422.

### Editorial workflow

**Files:** `test_editorial.py`, `test_releases_and_checks.py`

The catalog is maintained by editors via a two-person review and a release pipeline:

- **Role enforcement** (`test_editorial.py`): `/v1/editor/*` routes require a token carrying `hq_role=editor`. Other roles (including the parent role) receive 403.
- **Lifecycle** (`test_editorial.py`): A habit goes from `draft` → `in_review` → `approved`. The author cannot approve; the reviewer cannot reject with a self-review.
- **Lint** (`test_releases_and_checks.py`): Submitting a habit with coercive wording, unknown goals, or age-inconsistent parameters is blocked with named reason codes; the record stays `draft`.
- **Release, diff, validate, publish** (`test_editorial.py`): A release is `built`, then `diff`ed against the current release, then `validate`d for required chapters, then `publish`ed by a separate editor identity. Validation errors block publication.
- **Rollback** (`test_editorial.py`): Rolling back to a prior source release re-publishes its items.
- **Withdrawal** (`test_editorial.py`): Withdrawing a habit flags all plans that contain it so parents cannot confirm until they review the change.

### Hosting and adapters

**File:** `test_hosting.py`

These prove that model-provider wiring and process hosting work without touching the actual provider:

- **Google Gemini adapter** (`test_hosting.py`): `BoundedChat` creates a `ChatGoogleGenerativeAI` with the right `model`, `google_api_key`, `temperature`, `timeout` and tool binding.
- **OpenAI-compatible adapter** (`test_hosting.py`): Same contract for the generic `ChatOpenAI` wrapper with Nebius/OpenRouter-style endpoints.
- **Ollama adapter** (`test_hosting.py`): Same contract for local `ChatOllama` with the correct `base_url` and timeout.
- **Validation** (`test_hosting.py`): Switching to `google` or `openai` mode without required settings raises Pydantic `ValidationError`.
- **In-API worker** (`test_hosting.py`): When `run_worker_in_api=True`, the FastAPI lifespan starts a daemon thread named `plan-worker` that processes the queue. After the `TestClient` context exits, the thread is gone.
- **Serve loop** (`test_hosting.py`): `worker.serve` survives a failed pass, stops cleanly when signaled, and does not leak the stop event.
- **OpenAPI completeness** (`test_hosting.py`): Every endpoint has a summary, a description of at least 40 characters, and a declared tag.

### Final boundaries and durability

**File:** `test_final_boundaries.py`

These put stress on the edges where publishing must be safe:

- **Confirmation is fenced** (`test_final_boundaries.py`): Confirmation grabs a row-level lock during `validate_sources`; a concurrent catalog edit is blocked until confirmation finishes.
- **Deadline expiry before publish** (`test_final_boundaries.py`): If the row deadline passes after source validation, publication rolls back and the job is marked failed with zero proposals written.
- **Harmful concerns** (`test_final_boundaries.py`): "Punish the child" is caught before model invocation; the job fails with zero proposals.
- **Missing chapter without invention** (`test_final_boundaries.py`): A release missing a required chapter causes the plan run to fail; the planner does not invent a replacement.
- **Slow model** (`test_final_boundaries.py`): A model response arriving after the time budget is rejected with `BudgetExceeded`.
- **Lease renewal and database failure** (`test_final_boundaries.py`): The lease renewal loop repeats then stops when fenced; it also survives database connection loss.

### Plan flags migration safety

**File:** `test_plan_flags_schema.py`

Database-level constraints and RLS for the `plan_flags` and `catalog_requests` tables:

- `plan_flags` has row-level security enabled; foreign-key isolation is enforced by `my_parent()`.
- The `kind` check constraint rejects unknown values.
- `catalog_requests` upserts correctly on repeated identical phrases.
- Parent profile edits that remove a required cue create a `cue_removed` flag for the affected active habits.
- Flag resolution is idempotent and household-scoped.
- Confirming a newer plan auto-resolves all older flags.

## pgTAP RLS test

`supabase/tests/rls.sql` is a pgTAP script that runs inside the database and proves three RLS invariants:

1. The `hq_api` role sees children in its own household and none in another.
2. The `hq_api` role cannot update reviewed catalog rows or read draft (pre-publication) tables.
3. The `hq_editor` role cannot read household tables and cannot publish a release it built itself.

The script runs inside a transaction that is rolled back on completion; it can be executed by any user with `CREATE` privileges on the test database.

## Scripted demo

`scripts/demo.py` is not a unit test; it is a **functional walkthrough** of the parent and editor flows against a running API:

- `uv run python scripts/demo.py` — basic: onboard a child, plan, review the proposal and confirm.
- `--scenario edits` — parent edits (move cue, promote, demote, remove goal) and the rules that refuse them.
- `--scenario second-round` — profile change (add morning cue), new pain point, habit promotion.
- `--scenario editor` — draft, review, publish and emergency withdrawal.
- `--scenario all` — run all of the above.
- `--remote --base-url <url>` — run against a deployed instance.

All demo expectations assume the seeded release 1 catalog and `MODEL_MODE=fake`. The script exercises its own worker via `python -m habitquest_backend.worker --once` for each scenario unless `--remote` is passed.

## Summary of what runs where

| Environment | What runs |
|---|---|
| CI / `pytest` | Lint, format, mypy, all pytest with fake model and postgres container, 100% branch coverage gate |
| Local with Supabase | `scripts/smoke_supabase_auth.py` |
| Local with Ollama | `pytest -m live_local` (informational) |
| Local against running API | `scripts/demo.py` |
