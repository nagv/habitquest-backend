---
type: concept
title: Local bootstrap, migrations and Render deployment
description: How to bootstrap HabitQuest locally with either Supabase or native PostgreSQL, how forward-only migrations are checksummed and applied with advisory locking, how the seed catalog and checkpoint schema are set up, and how the Render Blueprint builds, starts and health-checks the service — including the single-service worker option and its idle-sleep limitation.
tags: [deployment, migrations, postgres, supabase, render, bootstrap, database, seed, health]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-30T00:13:47.181Z
sources:
  - id: openwiki-source-23775c3de52f3ab95a13cb8b
    resource: repo://README.md
  - id: openwiki-source-f70156010a8eb5325870bfc6
    resource: repo://render.yaml
  - id: openwiki-source-85ec7e034e282865816060fa
    resource: repo://scripts/bootstrap_local.py
  - id: openwiki-source-ad0239af68e5ec3322fd098a
    resource: repo://scripts/render_start.sh
  - id: openwiki-source-aac0765bbf153c93ec271d04
    resource: repo://scripts/setup_db.py
  - id: openwiki-source-70663512997ed0573d9f7f78
    resource: repo://src/habitquest_backend/main.py
  - id: openwiki-source-90ede806c69726976696c10f
    resource: repo://supabase/migrations/202610280001_onboarding.sql
  - id: openwiki-source-4721ccb7680d166ed187c4e4
    resource: repo://supabase/migrations/202610290006_editorial.sql
generated: { by: "openwiki/0.7.2", at: "2026-10-30T00:13:47.181Z" }
---

# Local bootstrap, migrations and Render deployment

HabitQuest can run on a developer machine via either Supabase on Docker or a native PostgreSQL instance, and on Render as a single-service Blueprint. The same application code is bootstrapped by [`scripts/bootstrap_local.py`](../../scripts/bootstrap_local.py), migrated by [`scripts/setup_db.py`](../../scripts/setup_db.py), and deployed by [`render.yaml`](../../render.yaml). The sections below walk through each layer.

## Local bootstrap: Supabase vs native PostgreSQL

`bootstrap_local.py` chooses between two paths based on whether `--native` is passed.

### Supabase path (default)

When Docker is available, the script runs `npx supabase start` and reads the database URL from `supabase status --output json`. It creates `habitquest_test` if it does not exist, then writes an `.env` file with `DATABASE_URL`, `TEST_DATABASE_URL`, `JWT_ISSUER`, `JWT_JWKS_URL`, and disables local demo authentication (`LOCAL_DEMO_AUTH=false`). The Supabase CLI itself applies migration files under `supabase/migrations`, so `setup_db.py` later treats them already-applied and skips re-running them.

### Native PostgreSQL path (`--native`)

When `--native` is passed, the script uses the PostgreSQL binaries on `PATH` to:

1. Run `initdb` in `.local/postgres` with SCRAM-SHA-256 authentication, a generated password stored in `.local/db-password`, and `no-locale` encoding.
2. Start the cluster on `127.0.0.1` at the requested port (default `55433`) if it is not already running.
3. Create `habitquest` and `habitquest_test` databases.
4. Write an `.env` file pointing at both databases, setting `MODEL_MODE=fake`, generating `JWT_SECRET` and `JWT_ISSUER`, and enabling local demo authentication (`LOCAL_DEMO_AUTH=true`).

Existing `.env` files are **preserved** — the script only writes one if the file is missing.

For both paths, the script finishes by invoking `scripts/setup_db.py` once per database:

```python
for url in urls:
    command([sys.executable, str(root / "scripts/setup_db.py")],
            env={**os.environ, "DATABASE_URL": url})
```

The generated `DATABASE_URL` uses a password-authenticated `habitquest` account, so `bootstrap_local.py` does not itself need `CREATEROLE` or root access.

## Migration application: forward-only, checksummed and advisory-locked

[`scripts/setup_db.py`](../../scripts/setup_db.py) is the only migration mechanism. It is intentionally small and runs entirely in-process.

### Control flow

<!-- openwiki: mermaid parse failed and this diagram was converted to a text fence so it does not break rendering. Fix the diagram source and restore the mermaid fence. Parser error: Heuristic: a semicolon inside a label breaks rendering; rephrase the label. -->
```text
flowchart TD
    A["SELECT pg_advisory_xact_lock(28102026)"] --> B["CREATE TABLE IF NOT EXISTS public.hq_migrations"]
    B --> C["For each *.sql in supabase/migrations, sorted by name"]
    C --> D["Compute SHA-256 digest"]
    D --> E{Recorded?}
    E -- yes --> F{Digest matches?}
    F -- yes --> C
    F -- no --> G["RuntimeError: Applied migration changed"]
    E -- no --> H{Already applied by Supabase CLI?}
    H -- yes --> I["INSERT digest; skip SQL"]
    H -- no --> J["Execute SQL text"]
    J --> I
    I --> C
    C --> K["GRANT hq_api, hq_worker, hq_editor TO CURRENT_USER"]
    K --> L["Run supabase/seed.sql"]
    L --> M["PostgresSaver.setup() in hq_checkpoint"]
    M --> N["GRANT hq_worker on hq_checkpoint.*"]
```

### Invariants and failure modes

1. **Advisory lock serialization.** `pg_advisory_xact_lock(28102026)` is taken on every run, so two processes (for example two Render instances booting simultaneously) cannot apply the same file twice.
2. **Forward-only.** There is no rollback, down-migration or revert. A bad change is corrected by a later migration file.
3. **Never reset an existing database.** No `DROP DATABASE`, no `DROP SCHEMA CASCADE`, no `TRUNCATE`. The script adds and modifies but never destroys.
4. **Discourage drift with a digest check.** If `hq_migrations` already records a filename but its SHA-256 differs from the current file, the script raises `RuntimeError`. This turns "I edited an already-applied migration" into a startup failure before any data is touched.
5. **Supabase CLI recognition.** Before executing a new file, the script checks `supabase_migrations.schema_migrations` by the numeric prefix of the filename. If the Supabase CLI already applied the migration, the SQL is skipped but the digest is still recorded, so later native runs treat it as tracked.

### Role creation and why `CREATEROLE` is required

Migrations create `hq_api`, `hq_worker` and `hq_editor` as `NOLOGIN` roles guarded by `pg_roles` lookups. Because these are unconditional `CREATE ROLE` calls, the database login that runs `setup_db.py` must have the `CREATEROLE` privilege. After applying migrations, the script unconditionally issues:

```sql
GRANT hq_api, hq_worker, hq_editor TO CURRENT_USER;
```

This lets the connecting login switch into those roles via `SET LOCAL ROLE`, which all application transactions do. Without this grant, every `SET LOCAL ROLE hq_api` or `SET ROLE hq_worker` would fail.

## Seed catalog and checkpoint schema setup

After migrations and grants, the script runs `supabase/seed.sql`. The file is idempotent: it starts with `IF EXISTS(SELECT 1 FROM hq.catalog_releases WHERE id = 1) THEN RETURN`, so re-running the setup is safe.

The seeded development catalog includes goals, concerns, concerns-to-goals mappings, habit registry entries, habit versions with levels, tags, variants, chapter templates, and a single published release assembling them. This data is for development only; the README notes it is not reviewed for a family pilot.

Finally, the script creates the LangGraph checkpoint infrastructure:

```python
with PostgresSaver.from_conn_string(make_conninfo(url, options="-c search_path=hq_checkpoint")) as saver:
    saver.setup()
```

`PostgresSaver.setup()` creates the tables inside the `hq_checkpoint` schema, which itself was created empty in the onboarding migration. The script then grants `hq_worker` usage on the schema and full CRUD on all tables inside it. The worker later opens a dedicated connection with `SET search_path TO hq_checkpoint` for checkpoint persistence.

## Render Blueprint build, start and health checks

[`render.yaml`](../../render.yaml) defines the production blueprint.

### Build command

```bash
pip install uv==0.12.23 && uv export --frozen --no-dev --no-emit-project -o requirements.txt && pip install -r requirements.txt && pip install --no-deps .
```

The lockfile is exported to a plain `requirements.txt` and installed with `pip`. `uv` is only a build tool on Render; it is not present at runtime.

### Start command

[`scripts/render_start.sh`](../../scripts/render_start.sh) is thin and serial:

```sh
set -eu
python scripts/setup_db.py
exec python -m uvicorn habitquest_backend.main:app --host 0.0.0.0 --port "${PORT:-8000}"
```

Migrations are applied **before** the server starts. Because the script replaces itself with `exec`, a migration failure aborts the container before it accepts traffic.

### Health check: `/health/ready`

Render is configured with:

```yaml
healthCheckPath: /health/ready
```

The handler runs `SELECT 1 FROM hq.children LIMIT 0` inside a database transaction. This query verifies two things: the database is reachable, **and** the migrations that create the `hq` schema and the `hq.children` table have been applied. If either fails, it returns HTTP 503. Render treats this as "not ready" and retries.

The sibling `/health/live` probe returns `{"status":"ok"}` without touching the database and is suitable for basic liveness checks.

### The `RUN_WORKER_IN_API` single-service option

`render.yaml` sets `RUN_WORKER_IN_API: "true"`, which causes the FastAPI lifespan to spawn the planning worker on a daemon thread beside the HTTP server:

```python
thread = threading.Thread(target=serve, args=(settings, stop), name="plan-worker", daemon=True)
thread.start()
```

This arrangement means one free Render service is enough to run both the API and the worker, but it comes with an operational caveat specific to Render's free tier:

> **A free Render instance sleeps when idle and stops the worker with it.**

When the service spins down from inactivity, the daemon thread is terminated. A plan run already enqueued stays in `queued` state. It only resumes when the next HTTP request wakes the process and the lifespan restarts the thread. This is why `scripts/demo.py --remote` polls for results instead of running a local worker, with a comment that "a remote worker may be waking from sleep."

The production alternative, commented out in `render.yaml`, is `RUN_WORKER_IN_API=false` plus a separate worker service running `python -m habitquest_backend.worker`. That requires a paid Render plan but keeps model calls off the request process, allows independent scaling, and eliminates the idle-sleep problem.

## Environment group

The `habitquest-settings` env-var group in `render.yaml` holds provider keys and authentication values. Provider secrets (`GOOGLE_API_KEY`, `OPENAI_API_KEY`) are marked `sync: false` so the Render dashboard treats them as sensitive and does not echo them in build logs. `LOCAL_DEMO_AUTH` must remain `false` on any hosted service, because the local token scripts are not intended for production use.

## Related pages

- [Data model, migrations and persistence](../architecture/data-model-and-persistence.md) — full schema description and the migration history.
- [Runtime topology: API process, worker loop and job leases](../architecture/runtime-and-processes.md) — how the worker thread behaves and how job leases work.
- [Settings and environment configuration](configuration.md) — every `Settings` field, validation rule and consumer.
