# HabitQuest onboarding backend

Child profile + parent concerns -> goals mapped from the reviewed catalog -> ranked, agent-picked and reviewed habits and chapter outline -> parent-confirmed, versioned plan. Parents can add new concerns for the same child later.

This is the first backend slice. Daily occurrences, completion approvals, Spark transactions, screen-time enforcement, world rendering, redemption and formal RAGAS/LangSmith experiments are later integrations. Proposed point fields are catalog metadata, not awards.

## Stack

Python 3.12, uv, FastAPI/Pydantic, Supabase/Postgres/Auth, psycopg, Deep Agents on LangGraph, PostgreSQL checkpoints. One restricted Director delegates to a catalog Planner; the application always runs the final Reviewer. Chapter wording uses reviewed templates. No shell, arbitrary SQL, web browsing, generic subagent or filesystem tool is exposed to the model.

`MODEL_MODE=fake` is a deterministic, rule-based test double exercising the real graph and tools. It is not evidence of language understanding or recommendation quality. `MODEL_MODE=ollama` uses a real local tool-calling model. The production provider remains open behind the adapter.

## Quick start: local Supabase

Prerequisites: uv, Python 3.12, Node.js, Docker Desktop running. No hosted account is needed.

```powershell
uv sync --locked
npm ci
uv run python scripts/bootstrap_local.py
uv run python -m uvicorn habitquest_backend.main:app --reload --host 127.0.0.1
```

In another terminal in the same folder:

```powershell
uv run python -m habitquest_backend.worker
```

Open http://127.0.0.1:8000/docs. Create/sign in to a parent through **local Supabase Auth**, then paste the access token into Authorize. Local email confirmation is disabled in this development config. Keep production authentication configuration separate. Local email appears in the Supabase inbox; no external mail service is configured.

For a Supabase version using legacy symmetric signing, explicitly set the matching local `JWT_SECRET`; otherwise JWKS validation supports ES256/RS256. Never use a service-role key as a parent access token. The `hq` schema is not exposed through the Data API.

## Alternative: native PostgreSQL, without Docker

This preserves the same application schema, RLS roles and checkpoint code. It does **not** emulate Supabase Auth or Storage. Development-only signed tokens substitute for Auth so the backend mechanics can run locally.

Install PostgreSQL and put its `bin` folder on PATH. Then:

```powershell
uv sync --locked
uv run python scripts/bootstrap_local.py --native
uv run python -m uvicorn habitquest_backend.main:app --reload --host 127.0.0.1
```

The bootstrap creates a loopback-only, password-authenticated cluster under `.local/postgres`, application and test databases, and a git-ignored `.env`. It preserves existing `.env` files. Default port: 55433. Data remains there across restarts. To stop it:

```powershell
pg_ctl -D .local/postgres -w stop
```

With the API running, the synthetic demonstration invokes separate worker processes itself and checks every response:

```powershell
uv run python scripts/demo.py --scenario all
```

| Scenario | What it walks through |
|---|---|
| `basic` (default) | Onboard a child, describe pain points, review the suggested habits, goals and questions, confirm plan v1 |
| `edits` | Review-screen edits: a schedule change, then the refusals (`needs_cue`, `level`, `coverage`), removing a goal, demoting a habit, confirming |
| `second-round` | Answer the morning cue question in the profile, a new intake while the plan is full (`max_active_habits`), rotate a habit out and the morning habit in, confirm plan v2 |
| `editor` | Two editors: draft, lint refusal, review queue, self-approval refused, approval, release build/diff/validate, self-publication refused, publication, withdrawal |

Each step prints `ok` with its status, or stops with `FAIL` and the response body. Expectations assume the seeded catalog and `MODEL_MODE=fake`; the editor scenario publishes and then withdraws a uniquely named demo habit, so it can be rerun. Do not run an additional continuously polling worker during the demo.

For manual `/docs` usage, paste a short-lived native-local token into Authorize:

```powershell
uv run python scripts/local_token.py              # the demo parent
uv run python scripts/local_token.py --editor     # editor 1: drafts, submits, builds releases
uv run python scripts/local_token.py --reviewer   # editor 2: approves and publishes
uv run python scripts/local_token.py --sub 22222222-2222-2222-2222-222222222222  # a second parent
```

`--minutes` sets the lifetime (1-60, default 15). The two editor identities exist because a reviewer must differ from the author and a publisher from the release builder. These token utilities require `LOCAL_DEMO_AUTH=true` and are not for hosted services.

## Real local inference

Install Ollama and download a model that supports tool calling, such as the configured candidate `qwen3:8b`. Hardware suitability and full goal/planning quality must be verified on your machine.

```powershell
ollama pull qwen3:8b
```

Set `MODEL_MODE=ollama` in `.env` for both API and worker. The fake path needs no download or paid API key. Model/network errors have bounded retries; invalid selections and rejected reviews fall back to the reviewed deterministic template (`fallback: true`); budget, deadline, cancellation and unsafe requests fail explicitly. No unreviewed generated content is published.

## Deploying to Render

`render.yaml` is a Blueprint for one free web service plus Render Postgres in `virginia`:

- Build: `uv export` of the lockfile, installed with `pip` into Render's own environment (no `uv` at runtime).
- Start: `scripts/render_start.sh` applies pending migrations (idempotent and advisory-locked), then starts uvicorn on `$PORT`.
- `RUN_WORKER_IN_API=true` runs the planning worker on a thread inside the API process, so a single free service is enough. A free service sleeps when idle and stops the worker with it. For production, set it to `false` and enable the commented `worker` service, which needs a paid instance.
- Health check: `/health/ready`.

Environment (the `habitquest-settings` group):

| Variable | Value |
|---|---|
| `MODEL_MODE` | `fake`; `google` with `GOOGLE_MODEL` (a Gemini model id your key can use) and `GOOGLE_API_KEY`; or `openai` for any OpenAI-compatible provider such as Nebius, with `OPENAI_BASE_URL` (the provider's endpoint), `OPENAI_MODEL` (a model that supports tool calling) and `OPENAI_API_KEY`. Keys are secrets: set them in the dashboard. Proposals record the model as `<mode>:<model id>`; `fallback: true` means the model's picks failed validation or review and the reviewed template was used |
| `JWT_SECRET`, `JWT_ISSUER` | Dev HS256 tokens. Mint them locally with `scripts/local_token.py`, using the same values in your local `.env` plus `LOCAL_DEMO_AUTH=true`. For real users, use Supabase Auth instead: `JWT_ISSUER=https://<project>.supabase.co/auth/v1`, `JWT_JWKS_URL=.../.well-known/jwks.json`, no `JWT_SECRET`, and editors get `app_metadata.hq_role = "editor"` |
| `LOCAL_DEMO_AUTH` | Always `false` on the server |

Check a deployment with `uv run python scripts/demo.py --remote --base-url https://<service>.onrender.com --scenario all`. `--remote` polls for results instead of running a local worker. The first request after an idle period waits for the free service to wake up.

The migrations create the `hq_api`, `hq_worker` and `hq_editor` roles, so the database user needs `CREATEROLE`. Treat a hosted deployment as test data only until family deletion, retention and model-quality checks exist.

## API walkthrough

1. `POST /v1/children` with a profile: `name`, `age_band` (`6-9` or `10-13`), optional `interests`, `accessibility`, `opted_in_topics`, `preferred_world` (`space`, `forest`, `city`), `timezone` (IANA, stored on the household) and `routine_schedule`, e.g. `{"bedtime": {"cue": "20:00", "days": ["mon","tue","wed","thu","fri","sat","sun"]}}`. Anchors: `morning`, `after_school`, `evening`, `bedtime`.
2. `PATCH /v1/children/{id}` replaces the profile; send `expected_profile_version`. Profile edits never change a confirmed plan; the next intake uses them.
3. `POST /v1/children/{id}/intakes` with `concerns` (free text) and `available_minutes` for this round. Add a new intake whenever the parent has new pain points.
4. `POST /v1/intakes/{id}/plan-runs` with an `Idempotency-Key` header; poll `/v1/plan-runs/{run_id}`.
5. `GET /v1/proposals/{run_id}`: goals mapped from the concerns (with the quoted evidence, confidence and a `suggested_not_stated` flag), active habits with cue and days, future candidates, shortlists with score breakdowns, routine links, chapter outline, unresolved goals (`needs_cue`, `no_catalog_entry`, `deferred`), questions, rationale and review. `fallback: true` means the reviewed deterministic template was used.
6. Optional edits: `PATCH /v1/proposals/{run_id}` with one `edit` (`remove_goal`, `reorder_goals`, `change_level`, `change_schedule`, `swap_habit`, `demote_active`, `promote_future`) and `expected_proposal_version`. Each edit is rebuilt from the reviewed release and re-checked: at most `max_active_habits` active habits, total minutes within the intake's `available_minutes`, a cue for every active habit's anchor, and no covered goal left uncovered. A violation returns `422` naming the rule (`max_active_habits:`, `available_minutes:`, `needs_cue:`, `coverage:`, `level:`, `stale:`, `chapter:`). A goal that fits the child's day but not the space left is `deferred` and its habit stays in `future`, so it can be promoted after demoting another.
7. `POST /v1/proposals/{run_id}/confirm` with `expected_plan_version` and `expected_proposal_version`. Confirmation revalidates every active habit and chapter against the current catalog release; a withdrawn or superseded item returns `409 replan_required`.
8. `GET /v1/children/{id}/plan` and `GET /v1/children/{id}/goals`.
9. `GET /v1/children/{id}/plan-flags` lists open flags on confirmed plans (a removed routine cue, a withdrawn habit). `POST /v1/plan-flags/{id}/resolve` marks one reviewed; confirming a newer plan resolves the older plan's flags.

The planning job: the mapper model turns concerns into goal IDs from the current catalog release; deterministic eligibility and scoring build per-goal shortlists and a template plan; the Director/Planner pick from the shortlists; code validates every pick; the Reviewer must accept the final version. Invalid picks get one revision, then the reviewed template is published. Budget, deadline, cancellation and unsafe requests fail explicitly.

## Editorial API

Catalog staff use `/v1/editor/*` with a JWT whose `app_metadata.hq_role` is `editor` (assigned by an administrator, never by the user). These requests run as the `hq_editor` database role, which can read and write catalog drafts but has no access to household tables.

1. `GET /v1/editor/{habits|goals|concerns|chapters}?status=in_review` is the review queue (any status, or all versions without it). `POST /v1/editor/{habits|goals|concerns|chapters}/{id}/versions` starts an entity or a new draft version of an approved one (one open draft per entity); `PUT .../versions/{n}` replaces a draft; `GET .../versions/{n}` shows it with its audit trail.
2. `POST .../versions/{n}/submit` runs the deterministic checks (levels, age bounds, linked goals exist, lexical lint for punishment, secrecy, dangerous activity, weight loss, diagnosis or treatment and coercive wording) and returns `422` with reason codes, or moves the draft to `in_review`, where it is frozen.
3. `POST .../approve` or `.../reject` (with `reason`, back to draft) by an editor other than the author. Approved versions are immutable.
4. `POST /v1/editor/releases` builds a candidate from the latest approved, non-withdrawn versions, or with `source_release_id` copies a published release (rollback). `GET /v1/editor/releases/{id}/diff` compares it with the current release; `GET .../validate` returns `errors` (missing world × goal chapters, references outside the release, concerns without goals, withdrawn habits) and `warnings` (a goal with no habit for an age band).
5. `POST /v1/editor/releases/{id}/publish` by an editor other than the builder; validation errors block it. Publishing switches the current release; open proposals built on the old one get `409 replan_required` at confirmation if their habits changed.
6. `POST /v1/editor/habits/{id}/withdraw` with `reason` withdraws every approved version immediately and permanently and adds a `plan_flags` row to each child's current plan that uses it.
7. `GET /v1/editor/catalog-requests` lists anonymised unmatched concern phrases.

The two-person rules, status transitions, release builds, publication and withdrawal run in `SECURITY DEFINER` database functions that refuse independently of the API.

## Tests and coverage

Every test run uses a **dedicated disposable test database**. Fixtures truncate HabitQuest tables in `TEST_DATABASE_URL`; never set it to a real household database. The native bootstrap creates `habitquest_test`. Supabase users should create a separate local test project/database, apply `scripts/setup_db.py`, then set `TEST_DATABASE_URL` accordingly.

```powershell
uv run python -m pytest --cov=habitquest_backend --cov-report=term-missing --cov-report=xml --cov-report=html
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run python -m mypy src
```

Coverage enforces 100% owned `src/habitquest_backend` statement/branch coverage; external dependencies and development scripts are outside that denominator. Real database/RLS, actual graph with scripted model, recovery, source revocation, budgets and confirmation concurrency have independent assertions. Worker subprocess coverage is collected. Tests require a database name ending in `_test` to reduce accidental use against application data. Code coverage does not prove exhaustive scenarios, child suitability or model quality. CI uses real Postgres and fake models, no cloud credentials.

With Docker/Supabase available, also run `npx supabase test db` for the supplied pgTAP policies and `uv run python scripts/smoke_supabase_auth.py` for real local signup/JWT validation. These require the Supabase runtime; native Postgres tests exercise RLS directly but do not claim Supabase Auth coverage.

## Persistence and guardrails

- SQL migrations are versioned and checksummed. The setup script also recognizes migrations applied by Supabase CLI. It never resets an existing database.
- API transactions switch to `hq_api`; workers to `hq_worker`; editor routes to `hq_editor`. RLS scope comes from the verified token. The local bootstrap connection is administrative; a hosted deployment must provision separate least-privileged login credentials for API and worker.
- Parent confirmation holds content locks through commit and revalidates source fields, policy and plan/session versions. Plan records are not changed in place.
- Leases fence publication; every lease has a separate checkpoint branch. Recovery copies a snapshot into the new branch, so an old worker cannot mutate the active branch.
- Model-call and conservative request-unit budgets persist across retries. `reserved_tokens` is a conservative serialized-input/output reservation, **not measured tokenizer usage or cost**. Run deadlines persist and are checked after model calls and at publication. Per-call output is capped for Ollama.
- Generated explanations are parent-facing. Child instructions and chapter content come from reviewed rows. The seeded lexical harmful-request check is only one layer; it is not a comprehensive safety classifier. Human-reviewed model-quality/security evals are required before a family pilot.
- External LangSmith tracing is disabled around model workflows, even if inherited tracing environment flags are set. Events store IDs, roles and outcome metadata rather than raw prompts.
- Checkpoints can contain private context. This local slice supports versioned profile correction, not complete family erasure, retention automation, or production privacy operations. Those are required before a family pilot.
- Initial migrations contain a small illustrative catalog. Treat release/point policy and content as development fixtures until reviewed for a real pilot.

See `docs/implementation-ledger.md` for decisions, verification evidence and known environment limitations. No cloud deployment or provider purchase is performed.

## Verification on the implementation host

The native PostgreSQL path, actual API/worker demo, 79 automated tests and 100% owned application statement/branch coverage were verified locally. Ruff and Python-module mypy checks passed. The Windows mypy executable launcher was blocked; the documented Python module entrypoint worked. The Supabase CLI launcher failed with spawn UNKNOWN on this host, and Docker/Ollama are absent; Supabase runtime/Auth, pgTAP and real-model smoke checks remain unverified. The pinned npm dependencies reported zero audit findings. No cloud deployment was performed.
