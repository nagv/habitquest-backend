---
type: concept
title: Settings and environment configuration
description: >
  Exhaustive reference for the Pydantic Settings class that drives HabitQuest runtime.
  Covers every field with its default, bounds, consumer, model-mode cross-field validators,
  .env file loading, and how render.yaml and scripts/bootstrap_local.py supply values.
tags: [configuration, environment, pydantic, settings, deployment]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-30T00:13:47.181Z
sources:
  - id: openwiki-source-5f5b95b3d6a215fa02ceb945
    resource: repo://.env.example
  - id: openwiki-source-f70156010a8eb5325870bfc6
    resource: repo://render.yaml
  - id: openwiki-source-85ec7e034e282865816060fa
    resource: repo://scripts/bootstrap_local.py
  - id: openwiki-source-ad0239af68e5ec3322fd098a
    resource: repo://scripts/render_start.sh
  - id: openwiki-source-fe99698257a6dda217ed54c6
    resource: repo://src/habitquest_backend/auth.py
  - id: openwiki-source-1d39ca689a3e1562bb21214c
    resource: repo://src/habitquest_backend/config.py
  - id: openwiki-source-2619b84336f41314b5cae39c
    resource: repo://src/habitquest_backend/db.py
  - id: openwiki-source-70663512997ed0573d9f7f78
    resource: repo://src/habitquest_backend/main.py
  - id: openwiki-source-5891884c8188a825b0aaec3e
    resource: repo://src/habitquest_backend/planning.py
  - id: openwiki-source-6e803a3ec70ccc9d4eb9e11b
    resource: repo://src/habitquest_backend/worker.py
generated: { by: "openwiki/0.7.2", at: "2026-10-30T00:13:47.181Z" }
---

# Settings and environment configuration

HabitQuest is configured through a single Pydantic `Settings` class that reads from the process environment and an `.env` file. This page documents every field, its validation rules, the consumers that depend on it, and the three paths that inject values.

## The `Settings` class

The canonical definition lives in [`src/habitquest_backend/config.py`](../../src/habitquest_backend/config.py).

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
```

When `create_app` starts it resolves `settings = settings or Settings()`, which triggers Pydantic Settings to load. Pydantic reads variables from the `.env` file and falls back to the process environment. Because `extra="ignore` is set, **unknown variables in the environment are silently accepted** rather than raising a validation error.

## Field reference

Below is the complete set of fields with defaults and constraints. All values are strings unless otherwise noted.

### Database

| Field | Default | Consumer |
|-------|---------|----------|
| `database_url` | `postgresql://postgres:postgres@127.0.0.1:54322/postgres` | `db.transaction`, `main.create_app`, `worker.main` |

### Model provider

| Field | Default | Constraint | Consumer |
|-------|---------|------------|----------|
| `model_mode` | `"fake"` | One of `fake`, `ollama`, `google`, `openai` | `planning` (selects `ChatGoogleGenerativeAI`, `ChatOpenAI`, `ChatOllama`, or fake) |
| `ollama_model` | `"qwen3:8b"` | | `planning` (when `model_mode="ollama"`) |
| `ollama_base_url` | `"http://127.0.0.1:11434"` | | `planning` |
| `google_model` | `""` | | `planning` (required when `model_mode="google"`) |
| `google_api_key` | `None` | `SecretStr \| None` | `planning` (required when `model_mode="google"`) |
| `openai_base_url` | `""` | | `planning` (required when `model_mode="openai"`) |
| `openai_model` | `""` | | `planning` (required when `model_mode="openai"`) |
| `openai_api_key` | `None` | `SecretStr \| None` | `planning` (required when `model_mode="openai"`) |

### Worker scheduling

| Field | Default | Constraint | Consumer |
|-------|---------|------------|----------|
| `run_worker_in_api` | `False` | `bool` | `main.create_app` (spawns worker thread if true) |
| `lease_seconds` | `180` | `ge=10`, `le=900` | `worker.run_once` (heartbeat frequency) |
| `max_attempts` | `3` | `ge=1`, `le=5` | `worker.run_once` (retry ceiling) |
| `max_model_calls` | `12` | `ge=1`, `le=30` | `worker.charge` (LM call budget) |
| `max_run_tokens` | `128000` | `ge=1024`, `le=256000` | `worker.charge` (token budget) |
| `max_run_seconds` | `120` | `ge=1`, `le=600` | `planning` (timeout per tool/model call) |

### Authentication

| Field | Default | Consumer |
|-------|---------|----------|
| `jwt_issuer` | `"http://127.0.0.1:54321/auth/v1"` | `auth.verify_token` |
| `jwt_audience` | `"authenticated"` | `auth.verify_token` |
| `jwt_secret` | `None` | `SecretStr \| None` | `auth.verify_token` (HS256 local mode) |
| `jwt_jwks_url` | `"http://127.0.0.1:54321/auth/v1/.well-known/jwks.json"` | `auth.verify_token` (ES256/RS256 JWKS mode) |

## Cross-field validators

After individual fields are parsed, a `model_validator(mode="after")` named [`google_configured`](../../src/habitquest_backend/config.py#L33) enforces provider completeness:

- If `model_mode` is `"google"`, then `google_model` must be non-empty and `google_api_key` must be set.
- If `model_mode` is `"openai"`, then `openai_base_url`, `openai_model`, and `openai_api_key` must all be set.

Violations raise `ValueError` at import time, so a misconfigured process fails immediately.

## How configuration values are supplied

There are three ways values reach the running process.

### 1. Local development via `.env`

The recommended workflow is to copy [`.env.example`](../../.env.example) to `.env` and edit as needed. The example file documents the standard local stack (PostgreSQL on port 54322, Ollama on port 11434) and sets `MODEL_MODE=fake`.

```bash
# Copy and edit
cp .env.example .env
```

The `.env` file variables override the built-in defaults. Because `extra="ignore"`, you can add unrelated variables (for example shell convenience exports) without causing validation errors.

### 2. `scripts/bootstrap_local.py`

[`bootstrap_local.py`](../../scripts/bootstrap_local.py) bootstraps a local PostgreSQL instance and writes a ready-made `.env` file if one does not already exist.

- In Supabase mode (the default), it starts Supabase with Docker, extracts the database URL from `supabase status --output json`, and writes `DATABASE_URL`, `TEST_DATABASE_URL`, `MODEL_MODE=fake`, `JWT_ISSUER`, `JWT_JWKS_URL`, `LOCAL_DEMO_AUTH=false`, and tracing flags.
- In `--native` mode, it initializes a local PostgreSQL cluster, creates `habitquest` and `habitquest_test` databases, and writes equivalent values plus a generated `JWT_SECRET` and `LOCAL_DEMO_AUTH=true`.

In both cases it **preserves an existing `.env`**; it only writes one if the file is missing.

### 3. Render deployment via `render.yaml`

[`render.yaml`](../../render.yaml) is the Render Blueprint. It declares:

- A **web service** (`habitquest-api`) that uses the free plan and runs the start command [`scripts/render_start.sh`](../../scripts/render_start.sh). This script applies database migrations with `python scripts/setup_db.py`, then starts the Uvicorn server on `${PORT:-8000}`.
- A **PostgreSQL database** (`habitquest-db`).
- An **env var group** (`habitquest-settings`) that holds provider secrets and toggles. The group is attached to the web service (and would be attached to a worker if uncommented). `sync: false` marks provider keys as sensitive.

`render.yaml` sets `RUN_WORKER_IN_API="true"` on the free plan. For production, the comment block shows how to set it to `"false"` and run a separate `habitquest-worker` service.

## Consumer map

Below is where each setting group is used at runtime.

```mermaid
flowchart TD
    subgraph Settings["Settings (config.py)"]
        DB["database_url"]
        AUTH["jwt_*"]
        MODEL["model_mode + provider keys"]
        WORKER["run_worker_in_api"]
        BUDGET["max_* / lease_seconds"]
    end

    subgraph Consumers
        DB --> DB_LAYER["db.transaction"]
        AUTH --> AUTH_LAYER["auth.verify_token"]
        MODEL --> PLANNING["planning (LLM switch)"]
        WORKER --> MAIN["main.create_app"]
        BUDGET --> WORKER_LAYER["worker.run_once + charge"]
    end
```

[`main.create_app`](../../src/habitquest_backend/main.py#L86) creates the `Settings` singleton and passes it down. The API layer acquires database connections via [`db.transaction`](../../src/habitquest_backend/db.py#L12), which uses `settings.database_url`. Token verification in [`auth.verify_token`](../../src/habitquest_backend/auth.py#L9) chooses between `jwt_secret` (HS256) and `jwt_jwks_url` (ES256/RS256). The planning module in [`planning.py`](../../src/habitquest_backend/planning.py#L138) branches on `model_mode`. The worker in [`worker.py`](../../src/habitquest_backend/worker.py#L81) consumes `lease_seconds`, `max_attempts`, `max_model_calls`, `max_run_tokens`, and `max_run_seconds` to throttle and budget each plan run.

## Security and secrets

All API keys (`GOOGLE_API_KEY`, `OPENAI_API_KEY`) and `JWT_SECRET` are typed as `SecretStr` in Pydantic. The application code extracts them with `.get_secret_value()` only when needed (for example in [`planning.py`](../../src/habitquest_backend/planning.py#L143) and [`auth.py`](../../src/habitquest_backend/auth.py#L13)). **Never log or print these values**.

`render.yaml` marks provider keys with `sync: false` so the Render dashboard treats them as sensitive and does not echo them in build logs.

## Variables not in `Settings`

Some variables appear in `.env.example`, `bootstrap_local.py`, and `render.yaml` but are **not fields on the `Settings` class**:

| Variable | Where it appears | Purpose |
|----------|-----------------|---------|
| `TEST_DATABASE_URL` | `bootstrap_local.py` only | Written alongside `DATABASE_URL` for pytest use. Not consumed by `Settings`. |
| `LOCAL_DEMO_AUTH` | `.env.example`, `bootstrap_local.py`, `render.yaml` | Enables the local demo auth route. Not a `Settings` field because the feature is set up externally. |
| `LANGSMITH_TRACING` / `LANGCHAIN_TRACING_V2` | `.env.example`, `bootstrap_local.py`, `render.yaml` | LangSmith/LangChain tracing toggles. Read directly by those libraries, not by HabitQuest code. |
| `PORT` | `render_start.sh`, `main.py` (via Uvicorn) | Render-injected HTTP port. Defaults to `8000` locally. |

## OpenWiki tool settings are separate

OpenWiki (this wiki system) has its own tool-level configuration in `~/.openwiki/.env`. Do **not** put provider API keys or `DATABASE_URL` values there. Keep HabitQuest secrets in the project `.env` (local) or in the Render env-var group (deployed), and keep OpenWiki tool settings in `~/.openwiki/.env` separate.
