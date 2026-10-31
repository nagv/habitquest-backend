# HabitQuest backend: guide for coding sessions

Read this first, then [docs/status.md](docs/status.md) (what is done and what is left). Source code and tests are
the truth; these docs summarise them.

## What this is

The API behind the HabitQuest app for children aged 3-13: families with one or more caregivers, children, a
pre-approved catalog of goals and habits, AI that maps a parent's pain points to goals, kids submitting
completions, caregivers approving them, Sparks (progress, never spent) and points (spent on rewards).

- Live: https://habitquest-api-qmt1.onrender.com (`/docs` for the API, `/openapi.json` for its contract).
- Frontend: `rahulwarekar20/habbit-quest` (React + Vite), being connected on a `feat/backend-api` branch that
  so far exists only on the owner's computer. How to connect it: [docs/frontend-integration.md](docs/frontend-integration.md).

## Where things are

| Path | What |
|---|---|
| `src/habitquest_backend/main.py` | App factory, health checks, dev login, catalog endpoints, router wiring, CORS |
| `auth.py` | Our HS256 tokens (caregiver and kid roles), Google ID-token check, PIN hashing, dev login |
| `accounts.py` | `/v1/me`, families, caregivers, invite codes, PIN, kid sessions |
| `onboarding.py` | Children, pain points, the plan (goals and habits), Sparks/levels (`progress`) |
| `ai.py` | Onboarding goal mapping: safety screen, one structured call, checks, keyword fallback |
| `llm.py` | The chat model through LangChain (any OpenAI-compatible endpoint, e.g. Nebius) |
| `coach.py`, `coach_tools.py` | The habit coach agent, its tools, suggestions accepted with the PIN |
| `insights.py` | Quest planner, weekly review, buddy lines |
| `guidance.py`, `guidance.json` | Parenting guidance and BM25 search |
| `safety.py` | Screening text children write and read |
| `checkins.py`, `extras.py` | Check-ins, surprises, kid extras, family settings |
| `limits.py` | Rate limits for sign-in and PIN attempts |
| `daily.py` | Today's habits, kid submissions, approvals |
| `rewards.py` | Redemptions, points history, the reward shop and requests |
| `db.py` | One transaction per request as `hq_api` (caregiver) or `hq_kid` (kid), with row-level security |
| `config.py` | Every setting (environment variables) |
| `db/migrations/*.sql` | The schema, in order. `db/seed.sql`: the catalog, upserted on every start |
| `tests/` | One file per area; `conftest.py` has the fixtures |
| `docs/` | `mvp-plan.md` (scope and steps), `status.md` (progress), `frontend-integration.md` (API contract) |

## How to check your work

Postgres must be running. Locally: `uv run python scripts/bootstrap_local.py` (native PostgreSQL, port 55433,
writes `.env`). Any PostgreSQL works if you set the URLs yourself.

```bash
uv sync --locked
DATABASE_URL=<test db url> uv run python scripts/setup_db.py   # apply migrations and seed
TEST_DATABASE_URL=<test db url> uv run python -m pytest        # needs 100% coverage
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run python -m mypy src
```

- `TEST_DATABASE_URL` must name a database ending in `_test`; fixtures empty its tables.
- Before a PR, also run the suite against a brand-new database, so the migrations are proven from scratch.
- For anything a client sees, start uvicorn and try the flow with the dev login (`DEV_LOGIN=true`,
  `DEV_LOGIN_PASSWORD` of 12+ characters, `JWT_SECRET`), then `curl` the endpoints.

CI (`.github/workflows/test.yml`) runs the same checks on PostgreSQL 17.

## Conventions

- One pull request per step, with tests at 100% statement and branch coverage. Every endpoint has a summary, a
  docstring of 40+ characters and a tag (a test enforces this, and counts the endpoints).
- **Never edit a migration that has been applied** (the setup script refuses a changed one). Add a new file
  `db/migrations/<YYYYMMDD><NNNN>_<name>.sql`.
- Security lives in the database as well as the API: row-level security on every family table, the `hq_kid`
  policies, the points trigger. When you add a table, add its policies and grants, and a test that another
  family (and a kid token) cannot reach it.
- When an endpoint's shape changes, update its tests, the README, [docs/frontend-integration.md](docs/frontend-integration.md)
  and [docs/status.md](docs/status.md) in the same PR.
- No force-pushes; no secrets in commits or docs (name the variable, never its value).

## Environment (no secrets here)

- Render workspace "My Workspace" (`tea-db4sglflot8c73crrk9g`), web service `habitquest-api`
  (`srv-dbht6pmc6qcc73esrrkg`, free plan, virginia), auto-deploys from `main`; start script
  `scripts/render_start.sh` applies migrations and the seed. Database `habitquest-db` (free plan):
  **expires 2026-11-28**.
- Render variables known to be set: `DATABASE_URL`, `JWT_SECRET`, `JWT_ISSUER`, `GOOGLE_CLIENT_ID`,
  `DEV_LOGIN`, `DEV_LOGIN_PASSWORD`. Not yet: `AI_MODE` (so onboarding uses the keyword stand-in),
  `CORS_ORIGINS` (defaults to `http://localhost:5180`). Leftovers from the old version (`MODEL_MODE`,
  `RUN_WORKER_IN_API`, maybe `OPENAI_*`) are ignored. Check the dashboard before relying on this list.
- Google Cloud project "HabitQuest": consent screen in Testing (only listed test users can sign in), web client
  `589389699921-ol224oo6m4svf9tts3enahorubm4lb1s.apps.googleusercontent.com` with origins `http://localhost`
  and `http://localhost:5180`.

## Decisions already made (don't reopen without the owner)

- **Lean MVP schema:** no version columns, no background jobs, no proposal or plan snapshots, a flat catalog
  edited through `db/seed.sql`. The earlier versioned design was removed on purpose (PR #9) to keep the MVP simple.
- **Families:** one or more caregivers, each with a role (father, mother, guardian, other), joining by invite code.
- **Google sign-in directly** (not Supabase): the frontend sends Google's ID token, we issue our own token.
  Fewer services and no client secret. Supabase could be added later by linking `users.google_sub`.
- **Kids have no accounts:** a caregiver starts kid mode on the device and gets a kid token for one child; the
  family PIN guards leaving kid mode.
- **AI suggests, parents decide:** AI maps pain points to 1-3 pre-approved goals and recommends habits; the parent
  picks habits from that goal's pre-approved list. The AI sees labels instead of names, and its answers are
  checked against the catalog. Provider: any OpenAI-compatible endpoint (Nebius), with keyword
  matching as the fallback.
- **Sparks and points are separate:** Sparks = approved completions (levels, tier unlocks); points = an
  append-only ledger. Kid tokens write it only through `hq.kid_ledger`.
- **Ages 3-13 as in the frontend;** habit ids, buddies and level thresholds match the frontend.

## Pitfalls

- The free Render instance sleeps; the first request can take about a minute.
- The sandbox's local Postgres can stop between turns; if every database test fails at connection, restart it.
- `hq.child_today` decides "today" in the family's time zone; don't use `current_date` for anything a child sees.
- Kid tokens and caregiver tokens are refused on each other's endpoints; kid writes are also limited by policy
  (own child, today, `submitted` only).
- The `openwiki/` generated pages were removed because they described the old design; `openwiki/INSTRUCTIONS.md`
  is the brief for regenerating them.
