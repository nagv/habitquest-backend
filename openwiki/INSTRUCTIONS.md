# OpenWiki brief: habitquest-backend

This file is the human-written scope for the generated wiki. OpenWiki reads it on every run and does not rewrite it.

## Audience and goal

Engineers and coding agents changing or operating this backend. Each page should let them answer "what does this do, where is it in the code, what does it depend on and what does it talk to" without reading the whole repository. Cite file paths (and functions) for every claim. Do not invent endpoints, settings, tables or hosts that are not in the code; when something is not implemented, say so.

## What the system is

A Python 3.12 FastAPI backend. Parents describe a child's pain points; a planning job maps them to goals from a versioned, two-person-reviewed habit catalog, ranks eligible habits deterministically, lets LangGraph Deep Agents pick within code-enforced limits, and has a mandatory Reviewer check the result. Parents edit and confirm the proposal as an immutable plan. Catalog editors maintain the catalog through draft, review and release steps.

## Required pages

1. **Overview and flows.** Parent flow: onboard child → intake (pain points) → plan run → poll → proposal → edits → confirm → later intakes. Editor flow: draft → submit (checks and lint) → approve by a second editor → build release → diff and validate → publish by a second editor → emergency withdrawal. Include a sequence diagram for each.
2. **Architecture and processes.** The API (`src/habitquest_backend/main.py`), the worker (`worker.py`: a separate process, or a thread inside the API when `RUN_WORKER_IN_API=true`), and how they coordinate only through Postgres (jobs, leases, fencing, checkpoints). Module map: `catalog/` (repository, eligibility, scoring, revalidation, releases), `editorial/` (api, checks, schemas), `planning.py`, `service.py`, `db.py`, `auth.py`, `config.py`.
3. **Planning pipeline.** Mapper → eligibility → scoring and shortlists → template plan → Director/Planner (`search_catalog` tool) → code validation (`validate_picks`) → Reviewer → one revision → template fallback (`fallback: true`). Budgets: model calls, reserved request units, deadlines. What each `MODEL_MODE` does (`fake`, `ollama`, `google`, `openai`).
4. **Data model.** Every table, view, trigger and function in `supabase/migrations/*.sql`, in migration order; the seed catalog in `supabase/seed.sql`. Database roles `hq_api`, `hq_worker`, `hq_editor`, which tables each may read or write, the RLS policies, and the `SECURITY DEFINER` functions (`transition_version`, `build_release`, `publish_release`, `withdraw_habit`, `lock_release`, `lock_intake`).
5. **Authentication and authorization.** HS256 tokens (`JWT_SECRET`) versus JWKS (`JWT_JWKS_URL`, Supabase Auth); the `app_metadata.hq_role = "editor"` claim; per-request database role and `app.parent_id` / `app.editor_id`; the two-person rules enforced in the database.
6. **API reference.** Every endpoint from the route summaries and docstrings in `main.py` and `editorial/api.py`, grouped by OpenAPI tag, with request body, success status, and error statuses. For `PATCH /v1/proposals/{id}`, list each edit op and the 422 rule names (`max_active_habits`, `available_minutes`, `needs_cue`, `coverage`, `level`, `stale`, `chapter`).
7. **Configuration.** A table of every field in `Settings` (`config.py`): environment variable name, default, which process reads it, and which mode it applies to. Note the validators (google and openai modes require their values). Note that secrets are never committed.
8. **Dependencies.** See the section below.
9. **Network calls and egress.** See the section below. This page is mandatory and must be exhaustive.
10. **Deployment.** `render.yaml` (Blueprint), `scripts/render_start.sh` (migrations then uvicorn), the build command, the single-service layout and the paid two-service alternative, health check `/health/ready`, and CI in `.github/workflows/`.
11. **Testing.** The 100% statement and branch coverage gate (`pyproject.toml`), the rule that `TEST_DATABASE_URL` must name a database ending in `_test`, how DB tests and pure tests are organised, and what the fake model does and does not prove.
12. **Local development.** `scripts/bootstrap_local.py` (native Postgres or local Supabase), `scripts/setup_db.py`, `scripts/local_token.py` (`--editor`, `--reviewer`, `--sub`, `--minutes`), `scripts/demo.py` scenarios (`basic`, `edits`, `second-round`, `editor`, `all`, `--remote`).
13. **Known limits and decisions.** From `docs/implementation-ledger.md` and the refinement sections of `docs/superpowers/specs/2026-10-29-habit-catalog-design.md`.

## Dependencies page

Derive it from `pyproject.toml` (`[project].dependencies` and `[dependency-groups].dev`). For every direct runtime dependency give: version floor, why this repository uses it, and the files that import it. Group them: web/API, database, agents and models, auth, scripts. List dev tools separately, and the Node tooling (`package.json`: Supabase CLI, used through `npx`). Flag dependencies that are present only to support one `MODEL_MODE`.

Verify against the code rather than copying; as of the last brief update the direct runtime dependencies were: `deepagents`, `fastapi[standard]`, `httpx`, `langchain`, `langchain-core`, `langchain-google-genai`, `langchain-ollama`, `langchain-openai`, `langgraph`, `langgraph-checkpoint-postgres`, `langsmith`, `psycopg[binary,pool]`, `pydantic`, `pydantic-settings`, `pyjwt[crypto]`, `python-dotenv`.

## Network calls and egress page

List every outbound connection the code can make, as a table: destination, protocol, which process makes it (API, worker, script, build/CI), the triggering code (file and function), when it happens, and the setting that configures or enables it. Then a short "egress allowlist by deployment" section: what must be reachable for the default fake mode, for each model mode, for JWKS auth, and for CI and Render builds.

Find calls by reading the code, not only this list: look for `psycopg.connect`, `PyJWKClient`, `ChatOllama`, `ChatGoogleGenerativeAI`, `ChatOpenAI`, `httpx`, `subprocess` and `npx`. As of the last brief update the known calls were:

- Postgres via `DATABASE_URL` (`db.py` `transaction`, `worker.py`, `scripts/setup_db.py`, `scripts/bootstrap_local.py`); LangGraph checkpoints use the same database.
- JWKS endpoint `JWT_JWKS_URL` (`auth.py` `verify_token`), only when `JWT_SECRET` is unset.
- Ollama at `OLLAMA_BASE_URL` (`planning.py` `BoundedChat._generate`), only with `MODEL_MODE=ollama`.
- Google Gemini API (`ChatGoogleGenerativeAI`), only with `MODEL_MODE=google`.
- An OpenAI-compatible endpoint at `OPENAI_BASE_URL`, e.g. Nebius (`ChatOpenAI`), only with `MODEL_MODE=openai`.
- The HabitQuest API itself from `scripts/demo.py` (`httpx`, `--base-url`) and the local Supabase API from `scripts/smoke_supabase_auth.py`; `scripts/bootstrap_local.py` runs `npx supabase`.
- Package indexes (PyPI, the uv installer) during the Render build and CI.

State explicitly: `MODEL_MODE=fake` (the default) makes no model calls; LangSmith tracing is disabled in code around planning runs (`worker.py`, `tracing_context(enabled=False)`); model calls come from the worker, which is the API process when `RUN_WORKER_IN_API=true`.

## Rules

- Never copy secret values; name variables only. `.env` files are not part of the repository.
- Prefer tables for settings, endpoints, dependencies and network calls.
- Mark anything verified only with the fake model or stubbed providers as such (Google and OpenAI-compatible modes have adapter-contract tests only).
- Keep pages current on `--update`: when code adds a dependency, an endpoint, a setting or an outbound call, update the matching page.
