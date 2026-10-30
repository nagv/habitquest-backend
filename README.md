# HabitQuest backend

The service behind the HabitQuest app. Families register one or more caregivers and their children, describe a
child's pain points, and pick habits from a pre-approved catalog. Kids submit completions, caregivers approve
them, and children earn **Sparks** (progress that is never spent) and **points** (spent on rewards the
caregiver records).

The MVP is being built in steps (see [docs/mvp-plan.md](docs/mvp-plan.md)). This release is step 1: the lean
schema, the seeded catalog, catalog endpoints, health checks and the development login.

## Stack

Python 3.12, uv, FastAPI and Pydantic, PostgreSQL with row-level security, psycopg, PyJWT.

## Data model

Eleven tables in the `hq` schema ([db/migrations](db/migrations)). There are no version columns.

| Table | Purpose |
|---|---|
| `users` | A signed-in person; `id` is the subject of our access tokens, `google_sub` links a Google account |
| `families` | The family: name, time zone, parent PIN hash, invite code for further caregivers |
| `caregivers` | Who cares for the family's children, with their `role` (`father`, `mother`, `guardian`, `other`) |
| `children` | Name, age (3-13), buddy, chosen world (`space`, `forest`, `city`) and routine times per anchor |
| `goals` | Pre-approved goals ("Calmer bedtimes") with keywords for matching pain points |
| `habits` | Pre-approved habits, one goal each: parent and kid wording, icon, routine anchor, ages, tier (1-3), points |
| `pain_points` | A parent's description of a problem, and how it was mapped to goals (`ai`, `keywords`, `manual`) |
| `child_goals` | Goals suggested for or chosen for a child, with the reason and recommended habits |
| `child_habits` | The child's plan: chosen habits with cue time and days; removed rows keep the history |
| `completions` | One per habit per day: the kid submits, a caregiver approves or rejects |
| `points_ledger` | Append-only points: `+` per approved completion, `-` per redemption with a note of what it was for |

Sparks are the number of approved completions; a child's level and unlocked habit tier follow from Sparks.
Points are the sum of the ledger.

**Access.** API requests run as the `hq_api` database role with the caller's user id. Row-level security
limits every family table to the families the caller is a caregiver in. The catalog is read-only to the API,
and the points ledger can only be added to.

**Catalog.** [db/seed.sql](db/seed.sql) holds 7 goals and 36 habits taken from the frontend's quest library.
It is applied on every start and upserts by id, so editing a row and redeploying updates it. To stop offering a
goal or habit, set `is_active = false`; rows are never deleted because plans and history refer to them.

## Run it locally

Prerequisites: uv, Python 3.12 and PostgreSQL (its `bin` folder on PATH).

```bash
uv sync --locked
uv run python scripts/bootstrap_local.py
uv run python -m uvicorn habitquest_backend.main:app --reload --host 127.0.0.1
```

The bootstrap creates a loopback-only cluster under `.local/postgres` (port 55433), the `habitquest` and
`habitquest_test` databases, and a git-ignored `.env` with a signing secret and a development login password.
It keeps an existing `.env`. Stop the cluster with `pg_ctl -D .local/postgres -w stop`.

To use another PostgreSQL instead, set `DATABASE_URL` and run `uv run python scripts/setup_db.py`.

Open http://127.0.0.1:8000/docs, click **Authorize**, and in the **OAuth2PasswordBearer** form enter username
`parent` and the `DEV_LOGIN_PASSWORD` from `.env` (leave `client_id` and `client_secret` empty). `parent:<any
label>` logs in as a separate parent, e.g. to check that families are isolated.

## Tests

Tests need a dedicated database whose name ends in `_test`; fixtures delete its data.

```bash
uv run python -m pytest
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run python -m mypy src
```

Coverage of `src/habitquest_backend` must be 100% (statements and branches). CI runs the same checks against
PostgreSQL 17.

## Deploying to Render

`render.yaml` is a Blueprint for one free web service plus Render Postgres in `virginia`.

- Build: `uv export` of the lockfile, installed with `pip`.
- Start: `scripts/render_start.sh` applies pending migrations and the catalog seed (idempotent and
  advisory-locked), then starts uvicorn on `$PORT`.
- Health check: `/health/ready`.

| Variable | Value |
|---|---|
| `JWT_SECRET`, `JWT_ISSUER` | Sign and check our access tokens |
| `GOOGLE_CLIENT_ID` | The Google OAuth web client id, for Google sign-in (used from step 2) |
| `DEV_LOGIN`, `DEV_LOGIN_PASSWORD` | `true` and a shared tester password of 12+ characters (set it in the dashboard). Setting `DEV_LOGIN=true` without a valid password stops the service from starting |

The first migration replaces the earlier versioned schema: it drops the old `hq` and `hq_checkpoint` schemas and
their data. The migration creates the `hq_api` role, so the database user needs `CREATEROLE`. Treat a hosted
deployment as test data only until family deletion and retention exist.
