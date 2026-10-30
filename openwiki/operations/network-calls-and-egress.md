---
type: operational-reference
title: Network Calls, Egress and Per-Deployment Allowlist
description: Exhaustive inventory of every outbound connection the HabitQuest backend can make, including the process and configuration that controls it, plus the allowlist required for each deployment shape.
tags: [network, egress, security, operations, deployment, configuration]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-30T00:13:47.181Z
sources:
  - id: openwiki-source-6d4b4e707b8d60b6ccfa3425
    resource: repo://.github/workflows/openwiki-update.yml
  - id: openwiki-source-4f2678f93d3fd3835f9f2909
    resource: repo://.github/workflows/test.yml
  - id: openwiki-source-f70156010a8eb5325870bfc6
    resource: repo://render.yaml
  - id: openwiki-source-85ec7e034e282865816060fa
    resource: repo://scripts/bootstrap_local.py
  - id: openwiki-source-d7ed0bb14d48547c9f500bad
    resource: repo://scripts/demo.py
  - id: openwiki-source-aac0765bbf153c93ec271d04
    resource: repo://scripts/setup_db.py
  - id: openwiki-source-739330f1287a2173c961b478
    resource: repo://scripts/smoke_supabase_auth.py
  - id: openwiki-source-fe99698257a6dda217ed54c6
    resource: repo://src/habitquest_backend/auth.py
  - id: openwiki-source-1d39ca689a3e1562bb21214c
    resource: repo://src/habitquest_backend/config.py
  - id: openwiki-source-2619b84336f41314b5cae39c
    resource: repo://src/habitquest_backend/db.py
  - id: openwiki-source-5891884c8188a825b0aaec3e
    resource: repo://src/habitquest_backend/planning.py
  - id: openwiki-source-6e803a3ec70ccc9d4eb9e11b
    resource: repo://src/habitquest_backend/worker.py
  - id: openwiki-source-f0a6e7dc03522b2682f88655
    resource: repo://tests/conftest.py
generated: { by: "openwiki/0.7.2", at: "2026-10-30T00:13:47.181Z" }
---

# Network Calls, Egress and Per-Deployment Allowlist

This page is a complete inventory of every outbound network connection the repository initiates, the process that makes it, the setting that controls it, the default value, and whether `MODEL_MODE=fake` suppresses it. It also provides a per-deployment allowlist for operators who need to know what must be reachable from a given environment.

> **TL;DR:** The only unavoidable outbound connections are the PostgreSQL database used by the API, worker and checkpoint saver, and build-time package downloads. All model-provider calls and JWKS fetches are gated by explicit settings and can be eliminated by choosing the fake model or local JWT secret respectively. The demo script and smoke tests make HTTP calls to local services, not external APIs.

---

## Call Inventory

Each table entry below lists a single outbound connection category. Travel through every row if you need to audit or restrict egress.

### 1. PostgreSQL (psycopg)

All application data, job queues and LangGraph checkpoints live in a single PostgreSQL database. Multiple processes connect using [`psycopg`](repo://pyproject.toml#L19).

| Process | File, line | Connection target | Controlling setting | Default value | Fake mode avoids? |
|---------|-----------|--------------------|---------------------|---------------|-----------------|
| API server (`transaction`) | [`src/habitquest_backend/db.py#L16`](repo://src/habitquest_backend/db.py#L16) | `settings.database_url` | `DATABASE_URL` | `postgresql://postgres:postgres@127.0.0.1:54322/postgres` | No |
| Worker (`transaction`) | [`src/habitquest_backend/worker.py#L40`](repo://src/habitquest_backend/worker.py#L40) | `settings.database_url` | `DATABASE_URL` | same | No |
| Worker (checkpoint) | [`src/habitquest_backend/worker.py#L150`](repo://src/habitquest_backend/worker.py#L150) | `settings.database_url` with `search_path=hq_checkpoint` | `DATABASE_URL` | same | No |
| Setup script | [`scripts/setup_db.py#L13`](repo://scripts/setup_db.py#L13) | `url` from `DATABASE_URL` env var | `DATABASE_URL` | N/A (must be set) | No |
| Setup script (PostgresSaver schema) | [`scripts/setup_db.py#L41`](repo://scripts/setup_db.py#L41) | same, via `make_conninfo(url, options="-c search_path=hq_checkpoint")` | `DATABASE_URL` | N/A | No |
| Bootstrap (Supabase branch) | [`scripts/bootstrap_local.py#L102`](repo://scripts/bootstrap_local.py#L102) | `data["DB_URL"]` after `npx supabase status` | Supabase CLI output | N/A | No |
| Bootstrap (native branch) | [`scripts/bootstrap_local.py#L85`](repo://scripts/bootstrap_local.py#L85) | `postgresql://habitquest:<password>@127.0.0.1:<port>/postgres` | `--port` argument (default 55433) | 55433 | No |
| Test fixtures | [`tests/conftest.py#L57`](repo://tests/conftest.py#L57) | `TEST_DATABASE_URL` | `TEST_DATABASE_URL` | `postgresql://hq_dev@127.0.0.1:55432/hq_test` | No |

The API and worker use a 3-second `connect_timeout` and impose a `10s` per-transaction `statement_timeout` via [`db.py#L17-L18`](repo://src/habitquest_backend/db.py#L17-L18). The worker's checkpoint connection runs with `autocommit=True` and `prepare_threshold=0` ([`worker.py#L150-L151`](repo://src/habitquest_backend/worker.py#L150-L151)) because the LangGraph `PostgresSaver` manages its own transaction boundaries.

### 2. Model Provider APIs

Planning runs invoke an LLM through LangChain wrappers inside [`BoundedChat._generate()`](repo://src/habitquest_backend/planning.py#L125). Which provider is selected depends on the runtime setting `MODEL_MODE`, validated in [`config.py`](repo://src/habitquest_backend/config.py#L11).

| Provider | Initiating process | File, line | Controlling settings | Default value | Fake mode avoids? |
|----------|-------------------|-----------|----------------------|----------------|-------------------|
| **Fake** (keyword heuristics) | Worker / any `generate()` caller | [`src/habitquest_backend/planning.py#L178-L179`](repo://src/habitquest_backend/planning.py#L178-L179) | `MODEL_MODE=fake` | `MODEL_MODE` defaults to `fake` | **Yes** (zero network) |
| **Ollama** (local) | Worker | [`src/habitquest_backend/planning.py#L167-L177`](repo://src/habitquest_backend/planning.py#L167-L177) | `MODEL_MODE=ollama`, `OLLAMA_MODEL`, `OLLAMA_BASE_URL` | `qwen3:8b`, `http://127.0.0.1:11434` | **Yes** (set `MODEL_MODE=fake`) |
| **Google Gemini** | Worker | [`src/habitquest_backend/planning.py#L138-L151`](repo://src/habitquest_backend/planning.py#L138-L151) | `MODEL_MODE=google`, `GOOGLE_MODEL`, `GOOGLE_API_KEY` | all empty | **Yes** (set `MODEL_MODE=fake`) |
| **OpenAI-compatible** (e.g. Nebius) | Worker | [`src/habitquest_backend/planning.py#L152-L166`](repo://src/habitquest_backend/planning.py#L152-L166) | `MODEL_MODE=openai`, `OPENAI_BASE_URL`, `OPENAI_MODEL`, `OPENAI_API_KEY` | all empty | **Yes** (set `MODEL_MODE=fake`) |

The [`config.py` validator](repo://src/habitquest_backend/config.py#L33-L40) raises `ValueError` if a production mode is chosen without the corresponding URL/key pair, so misconfiguration surfaces at startup rather than at first model call.

**Fake mode behavior.** `fake` mode uses deterministic regexs in [`FAKE_GOALS`](repo://src/habitquest_backend/planning.py#L72-L77) to simulate goal mapping. The planner, director and reviewer all follow hard-coded templates. No HTTP or socket connection leaves the process.

### 3. JWKS Fetch (JWT verification)

The application supports two JWT validation paths, selected at runtime by which fields are set.

| Path | Initiating process | File, line | Controlling setting | Default value | Fake mode avoids? |
|------|-------------------|-----------|---------------------|----------------|-------------------|
| HS256 symmetric (local tokens) | API + worker on every authenticated request | [`src/habitquest_backend/auth.py#L12-L14`](repo://src/habitquest_backend/auth.py#L12-L14) | `JWT_SECRET` | `None` | No (this is the *absence* of an outbound call) |
| ES256/RS256 via JWKS | API + worker on every authenticated request | [`src/habitquest_backend/auth.py#L16`](repo://src/habitquest_backend/auth.py#L16) | `JWT_JWKS_URL` | `http://127.0.0.1:54321/auth/v1/.well-known/jwks.json` | No (set `JWT_SECRET` instead) |

When `JWT_SECRET` is `None`, PyJWT's `PyJWKClient` is instantiated with `settings.jwt_jwks_url` and performs an HTTPS (or HTTP, for local Supabase) GET on every token verification. Setting `JWT_SECRET` to any non-empty string suppresses the fetch entirely and switches to `HS256` local tokens.

### 4. Demo Script HTTP Calls

The [demo script](repo://scripts/demo.py) drives a running API via `httpx`.

| Endpoint | File, line | Base URL | Controlling argument | Default | Fake mode avoids? |
|-----------|-----------|----------|----------------------|---------|-------------------|
| All demo endpoints | [`scripts/demo.py#L49`](repo://scripts/demo.py#L49) | `--base-url` | `--base-url` | `http://127.0.0.1:8000` | No (this is a test script, not the application) |

With `--remote`, the script polls the status endpoint with a 2-second sleep instead of spawning a local worker subprocess.

### 5. Supabase CLI / Smoke Test HTTP Calls

When using Supabase for local development, the following calls are made by scripts, not the running application.

| Tool | File, line | Command / URL | Purpose | Controlling flag |
|------|-----------|---------------|---------|------------------|
| Supabase CLI | [`scripts/bootstrap_local.py#L98-L99`](repo://scripts/bootstrap_local.py#L98-L99) | `npx supabase start` | Start local Postgres, Auth and API | `--native` skips this entirely |
| Supabase CLI | [`scripts/bootstrap_local.py#L99`](repo://scripts/bootstrap_local.py#L99) | `npx supabase status --output json` | Extract DB_URL and API_URL for .env | `--native` skips this |
| Supabase Auth API | [`scripts/smoke_supabase_auth.py#L20`](repo://scripts/smoke_supabase_auth.py#L20) | `POST {API_URL}/auth/v1/signup` | Verify local Supabase Auth token issuance | manual invocation |
| Supabase CLI | [`scripts/smoke_supabase_auth.py#L14`](repo://scripts/smoke_supabase_auth.py#L14) | `npx supabase status --output json` | Read local service endpoints | manual invocation |

These are developer-only convenience scripts; they do not execute in production.

### 6. Build-Time Package Downloads

The following outbound connections occur only during CI or the Render build phase, never at application runtime.

| Environment | File, line | What is downloaded | Method |
|-------------|-----------|--------------------|--------|
| CI (GitHub Actions) | [`.github/workflows/test.yml#L24-L25`](repo://.github/workflows/test.yml#L24-L25) | `uv` (Astral) binary | `astral-sh/setup-uv@v6` action |
| CI | [`.github/workflows/test.yml#L27-L31`](repo://.github/workflows/test.yml#L27-L31) | Python dependencies from `pyproject.toml` | `uv sync --locked`, `uv export`, `uv run` |
| Render build | [`render.yaml#L11`](repo://render.yaml#L11) | `uv==0.12.23`, then Python dependencies | `pip install uv==0.12.23` + `uv export` + `pip install -r requirements.txt` |
| OpenWiki update | [`.github/workflows/openwiki-update.yml#L31`](repo://.github/workflows/openwiki-update.yml#L31) | `openwiki@0.7.2`, `mermaid@11.16.0`, `jsdom@29.1.1` | `npm install --global` |
| OpenWiki update | [`.github/workflows/openwiki-update.yml#L27`](repo://.github/workflows/openwiki-update.yml#L27) | Node.js 22 | `actions/setup-node@v4` |

The `render.yaml` build also installs `pip`, `uv` and the full dependency tree from PyPI; there is no private registry involved.

### 7. Model Provider Dependencies as Build-Time Downloads

While the application runtime only contacts the provider when the matching `MODEL_MODE` is selected, the wheels are downloaded unconditionally during dependency resolution:

- [`langchain-google-genai`](repo://pyproject.toml#L13) (Google)
- [`langchain-ollama`](repo://pyproject.toml#L14) (Ollama)
- [`langchain-openai`](repo://pyproject.toml#L15) (OpenAI-compatible)

If you need a fully air-gapped build you'll need a private index that mirrors these packages.

### 8. OpenWiki's Own Model Call

The OpenWiki update workflow makes a network call to a model provider independently of the application backend.

| Process | File, line | Provider / endpoint | Controlling env vars |
|-----------|-----------|----------------------|----------------------|
| OpenWiki CLI | [`.github/workflows/openwiki-update.yml#L38-L40`](repo://.github/workflows/openwiki-update.yml#L38-L40) | Nebius (`deepseek-ai/DeepSeek-V4.1-Flash-1017`) | `OPENWIKI_PROVIDER=nebius`, `NEBIUS_API_KEY`, `OPENWIKI_MODEL_ID` |

This requires `github.com`, `registry.npmjs.org`, and the Nebius API base URL to be reachable from the GitHub Actions runner.

### 9. Tracing (LangSmith) — Optional

When `LANGSMITH_TRACING` or `LANGCHAIN_TRACING_V2` are `true`, LangSmith tracing is enabled. Rendering in CI explicitly sets both to `"false"` ([`render.yaml#L63-L66`](repo://render.yaml#L63-L66)). The worker explicitly suppresses it during model runs with `tracing_context(enabled=False)` ([`src/habitquest_backend/worker.py#L155`](repo://src/habitquest_backend/worker.py#L155)).

---

## Egress Decision Flow

<!-- openwiki: mermaid parse failed and this diagram was converted to a text fence so it does not break rendering. Fix the diagram source and restore the mermaid fence. Parser error: Heuristic: an unescaped angle bracket inside a label breaks rendering; rephrase the label. -->
```text
flowchart TD
    A["Application starts"] --> B{"MODEL_MODE?"}
    B -->|fake| C["No model calls<br/>local only"]
    B -->|ollama| D["Ollama at<br/>OLLAMA_BASE_URL"]
    B -->|google| E["Gemini via<br/>googleapis.com"]
    B -->|openai| F["OpenAI-compatible<br/>at OPENAI_BASE_URL"]

    A --> G{"JWT_SECRET set?"}
    G -->|yes| H["Local HS256<br/>no JWKS fetch"]
    G -->|no| I["Fetch JWKS from<br/>JWT_JWKS_URL"]

    A --> J["Postgres at<br/>DATABASE_URL"]
    J --> K["Api worker<br/>checkpoint saver"]
```

The main runtime egress decision tree. Build-time downloads and the demo script are excluded.

---

## Per-Deployment Allowlists

The following sections list exactly what must be reachable for each deployment shape. Use them when writing firewalls, VPC egress rules, or zero-trust policies.

### A. Offline / Automated Tests / Fake Mode

Zero application egress is required for a fully local fake-mode deployment, assuming packages are already installed.

- **Must reach:** `127.0.0.1:<port>` for PostgreSQL (default `5432`, Supabase default `54322`, test default `55432`)
- **May reach:** nothing else
- **Key settings:** `MODEL_MODE=fake`, `JWT_SECRET=<any non-empty string>`, `LANGSMITH_TRACING=false`
- **CI behavior:** GitHub Actions uses a `localhost` postgres:17 service container on port `55432` ([`.github/workflows/test.yml#L8-L19`](repo://.github/workflows/test.yml#L8-L19)). All tests pass without any external model or JWKS call.

### B. Local Ollama (Self-Hosted)

Used by developers who have downloaded an Ollama model and want a real LLM planner without leaving the local network.

- **Must reach:**
  - PostgreSQL (same as above)
  - Ollama HTTP endpoint, default `http://127.0.0.1:11434`
- **May reach:** nothing else (JWKS fetch is skipped if `JWT_SECRET` is set)
- **Key settings:** `MODEL_MODE=ollama`, `OLLAMA_BASE_URL=http://127.0.0.1:11434`, `JWT_SECRET=<any non-empty string>`

### C. Hosted with Google Gemini

- **Must reach:**
  - PostgreSQL (local or remote)
  - Google Gemini API endpoints (usually `generativelanguage.googleapis.com`)
- **Key settings:** `MODEL_MODE=google`, `GOOGLE_MODEL=<model-id>`, `GOOGLE_API_KEY=<key>`
- **Optional:** JWKS URL if using Supabase Auth (`JWT_SECRET` not set)

### D. Hosted with OpenAI-Compatible Endpoint (e.g. Nebius)

- **Must reach:**
  - PostgreSQL (local or remote)
  - Provider base URL (`OPENAI_BASE_URL`), e.g. Nebius, OpenAI, or any v1-compatible host
- **Key settings:** `MODEL_MODE=openai`, `OPENAI_BASE_URL=<url>`, `OPENAI_MODEL=<model>`, `OPENAI_API_KEY=<key>`
- **Optional:** JWKS URL if using Supabase Auth

### E. Supabase Local Development

When using `scripts/bootstrap_local.py` without `--native`, the Supabase Docker stack is managed via `npx`.

- **Must reach:**
  - `npm registry` (for `npx supabase` CLI, if not already cached)
  - Docker Hub (for `supabase/postgres`, `supabase/kong`, etc. images)
  - Local Supabase ports: Postgres `54322`, Auth/API `54321`
- **Key settings:** `DATABASE_URL` auto-populated from `npx supabase status`
- **Note:** The application itself does not call Supabase over the network; it connects to the local Postgres directly.

### F. Render (Hosted)

Render builds from `render.yaml` with the free single-service layout.

- **Must reach:**
  - PyPI / `files.pythonhosted.org` (build-time `pip install`)
  - Render-managed PostgreSQL (via `DATABASE_URL`, injected from the `habitquest-db` resource)
- **Key settings:**
  - `MODEL_MODE` defaults to `fake` in the `habitquest-settings` env group ([`render.yaml#L45-L46`](repo://render.yaml#L45-L46)). Switching to `google` or `openai` adds the corresponding provider endpoint as a required egress target.
  - `JWT_SECRET` is auto-generated ([`render.yaml#L57-L58`](repo://render.yaml#L57-L58)), so JWKS is not fetched.
  - `RUN_WORKER_IN_API=true` runs the worker thread locally; no separate worker service egress is needed in the free plan.

### G. CI (GitHub Actions)

- **Must reach:**
  - `github.com` and `raw.githubusercontent.com` (checkout)
  - `files.pythonhosted.org` and `pypi.org` (Python package resolution)
  - PostgreSQL on `127.0.0.1:55432` (from the `services.postgres` container)
- **Explicitly disabled:** `LANGSMITH_TRACING=false`, `LANGCHAIN_TRACING_V2=false` ([`.github/workflows/test.yml#L20-L21`](repo://.github/workflows/test.yml#L20-L21)). No model calls are made because tests run with `MODEL_MODE=fake`.

---

## Quick Reference: Settings That Gate Egress

| Setting | Gated egress | Required when |
|-----------|-------------|---------------|
| `MODEL_MODE` | Model provider API | always set; `fake` suppresses |
| `GOOGLE_API_KEY` | Google Gemini | `MODEL_MODE=google` |
| `OPENAI_BASE_URL` | OpenAI-compatible endpoint | `MODEL_MODE=openai` |
| `OPENAI_API_KEY` | OpenAI-compatible endpoint | `MODEL_MODE=openai` |
| `JWT_SECRET` | JWKS fetch | set to anything non-empty to suppress |
| `JWT_JWKS_URL` | JWKS fetch | used only when `JWT_SECRET` is empty |
| `OLLAMA_BASE_URL` | Local Ollama | `MODEL_MODE=ollama` |
| `DATABASE_URL` | PostgreSQL | always required |
| `LANGSMITH_TRACING` / `LANGCHAIN_TRACING_V2` | LangSmith telemetry | set to `true` to enable |

---

## Related Pages

- [Architecture / Authentication and Authorization](../architecture/authentication-and-authorization.md) — how JWT verification works and why the JWKS path exists
- [Integrations / Model Providers](../integrations/model-providers.md) — how each provider is wired into the planning pipeline
- [Operations / Configuration](configuration.md) — full settings schema and `.env` templates
- [Operations / Dependencies](dependencies.md) — all Python and Node dependencies including provider SDKs
