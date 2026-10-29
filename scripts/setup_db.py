"""Apply tracked SQL migrations and framework tables to the explicitly selected database."""

import hashlib
import os
from pathlib import Path

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.conninfo import make_conninfo

root = Path(__file__).resolve().parents[1]
url = os.environ["DATABASE_URL"]
with psycopg.connect(url) as conn:
    conn.execute("SELECT pg_advisory_xact_lock(28102026)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS public.hq_migrations(name text PRIMARY KEY, digest text NOT NULL)"
    )
    for file in sorted((root / "supabase/migrations").glob("*.sql")):
        text = file.read_text(encoding="utf-8")
        digest = hashlib.sha256(text.encode()).hexdigest()
        old = conn.execute("SELECT digest FROM public.hq_migrations WHERE name=%s", (file.name,)).fetchone()
        if old:
            if old[0] != digest:
                raise RuntimeError("Applied migration changed: " + file.name)
        else:
            tracked = conn.execute("SELECT to_regclass('supabase_migrations.schema_migrations')").fetchone()[
                0
            ]
            applied = (
                tracked
                and conn.execute(
                    "SELECT 1 FROM supabase_migrations.schema_migrations WHERE version=%s",
                    (file.name.split("_")[0],),
                ).fetchone()
            )
            if not applied:
                conn.execute(text)
            conn.execute("INSERT INTO public.hq_migrations VALUES(%s,%s)", (file.name, digest))
    conn.execute("GRANT hq_api,hq_worker TO CURRENT_USER")
    conn.execute((root / "supabase/seed.sql").read_text(encoding="utf-8"))
with PostgresSaver.from_conn_string(make_conninfo(url, options="-c search_path=hq_checkpoint")) as saver:
    saver.setup()
with psycopg.connect(url) as conn:
    conn.execute("GRANT USAGE ON SCHEMA hq_checkpoint TO hq_worker")
    conn.execute("GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA hq_checkpoint TO hq_worker")
print("Migrations, reviewed seed catalog and checkpoint schema ready.")
