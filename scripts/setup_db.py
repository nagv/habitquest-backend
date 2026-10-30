"""Apply tracked SQL migrations and the catalog seed to the explicitly selected database."""

import hashlib
import os
from pathlib import Path

import psycopg

root = Path(__file__).resolve().parents[1]
url = os.environ["DATABASE_URL"]
with psycopg.connect(url) as conn:
    conn.execute("SELECT pg_advisory_xact_lock(28102026)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS public.hq_migrations(name text PRIMARY KEY, digest text NOT NULL)"
    )
    for file in sorted((root / "db/migrations").glob("*.sql")):
        text = file.read_text(encoding="utf-8")
        digest = hashlib.sha256(text.encode()).hexdigest()
        old = conn.execute("SELECT digest FROM public.hq_migrations WHERE name=%s", (file.name,)).fetchone()
        if old:
            if old[0] != digest:
                raise RuntimeError("Applied migration changed: " + file.name)
        else:
            conn.execute(text)
            conn.execute("INSERT INTO public.hq_migrations VALUES(%s,%s)", (file.name, digest))
    conn.execute("GRANT hq_api TO CURRENT_USER")
    conn.execute((root / "db/seed.sql").read_text(encoding="utf-8"))
print("Migrations and catalog seed applied.")
