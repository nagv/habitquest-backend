# OpenWiki brief: habitquest-backend

This file is the human-written scope for the generated wiki. OpenWiki reads it on every run and does not rewrite it.

## Audience and goal

Engineers and coding agents changing or operating this backend. Each page should let them answer "what does this do, where is it in the code, what does it depend on and what does it talk to" without reading the whole repository. Cite file paths (and functions) for every claim. Do not invent endpoints, settings, tables or hosts that are not in the code; when something is not implemented, say so.

## What the system is

A Python 3.12 FastAPI backend for the HabitQuest app, built in steps described in `docs/mvp-plan.md`. Families register caregivers and children, describe a child's pain points, and pick habits from a pre-approved catalog of goals and habits. Kids submit completions, caregivers approve them, and children earn Sparks (counted from approved completions) and points (an append-only ledger). The earlier versioned planning, worker and editorial code has been removed; pages about it should be deleted.

## Required pages

1. **Overview and roadmap.** What is implemented now versus the planned steps in `docs/mvp-plan.md`. Include a sequence diagram for each flow that is implemented.
2. **Architecture.** The API (`src/habitquest_backend/main.py`) and its modules (`accounts.py`, `auth.py`, `config.py`, `db.py`); how caregiver requests run as the `hq_api` database role with `app.user_id`, and kid requests as `hq_kid` with `app.child_id`, both set from the verified token; CORS (`CORS_ORIGINS`).
3. **Data model.** Every table, index, function and policy in `db/migrations/*.sql`, in migration order, with an ER diagram. Explain row-level security (`hq.my_user`, `hq.my_families`), the grants (catalog read-only, points ledger append-only), the check constraints, and how Sparks, levels and points are derived. Describe the seed catalog in `db/seed.sql` (goals, habits per goal, tiers and points) and how it is upserted on every start.
4. **Authentication and authorization.** Google sign-in (`POST /v1/auth/google`, `GOOGLE_CLIENT_ID`, the checks in `auth.py` `verify_google`); our HS256 tokens (`JWT_SECRET`, `JWT_ISSUER`, `JWT_AUDIENCE`) with the caregiver (`authenticated`) and `kid` roles; families, caregiver roles, invite codes and the family PIN (`accounts.py`); kid sessions; the `SECURITY DEFINER` functions `hq.google_user`, `hq.create_family` and `hq.join_family`; the development login (`DEV_LOGIN`, `DEV_LOGIN_PASSWORD`, `POST /v1/auth/token`, identities `parent` and `parent:<label>`). Include a sequence diagram of Google sign-in followed by creating or joining a family.
5. **API reference.** Every endpoint from the route summaries and docstrings in `main.py`, grouped by OpenAPI tag, with parameters, success status and error statuses.
6. **Configuration.** A table of every field in `Settings` (`config.py`): environment variable name, default and purpose. Note the validator (`DEV_LOGIN` requires `JWT_SECRET` and a 12+ character password). Note that secrets are never committed.
7. **Dependencies.** See the section below.
8. **Network calls and egress.** See the section below. This page is mandatory and must be exhaustive.
9. **Deployment.** `render.yaml` (Blueprint), `scripts/render_start.sh` (migrations and seed, then uvicorn), the build command, health check `/health/ready`, CI in `.github/workflows/`, and that the first migration drops the earlier schema.
10. **Testing.** The 100% statement and branch coverage gate (`pyproject.toml`), the rule that `TEST_DATABASE_URL` must name a database ending in `_test`, and how the schema tests check family isolation and data rules.
11. **Local development.** `scripts/bootstrap_local.py` (native PostgreSQL), `scripts/setup_db.py`, and logging in from `/docs`.

## Dependencies page

Derive it from `pyproject.toml` (`[project].dependencies` and `[dependency-groups].dev`). For every direct runtime dependency give: version floor, why this repository uses it, and the files that import it. List dev tools separately.

## Network calls and egress page

List every outbound connection the code can make, as a table: destination, protocol, which process makes it (API, script, build/CI), the triggering code (file and function), when it happens, and the setting that configures it. Find calls by reading the code: look for `psycopg.connect`, `PyJWKClient`, `httpx` and `subprocess`. As of the last brief update the known calls were:

- Postgres via `DATABASE_URL` (`db.py` `transaction`, `scripts/setup_db.py`, `scripts/bootstrap_local.py`).
- Google's signing keys at `https://www.googleapis.com/oauth2/v3/certs` (`auth.py` `google_key`, via PyJWT's `PyJWKClient`, cached for an hour), on `POST /v1/auth/google`.
- Package indexes (PyPI, the uv installer) during the Render build and CI.

## Rules

- Never copy secret values; name variables only. `.env` files are not part of the repository.
- Prefer tables for settings, endpoints, dependencies and network calls.
- Keep pages current on `--update`: when code adds a dependency, an endpoint, a setting, a table or an outbound call, update the matching page.
