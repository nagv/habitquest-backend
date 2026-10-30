from contextlib import contextmanager
from typing import Any
from uuid import UUID

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from habitquest_backend.config import Settings


@contextmanager
def transaction(settings: Settings, user: UUID | None = None, *, kid: UUID | None = None):
    """One transaction with row-level security: as `hq_api` scoped to `user`'s families, or with `kid`, as
    `hq_kid` scoped to that one child.
    """
    with psycopg.connect(settings.database_url, row_factory=dict_row, connect_timeout=3) as conn:
        conn.execute("SET LOCAL ROLE hq_kid" if kid else "SET LOCAL ROLE hq_api")
        conn.execute("SET LOCAL statement_timeout='10s'")
        conn.execute(
            "SELECT set_config('app.user_id',%s,true),set_config('app.child_id',%s,true)",
            (str(user or ""), str(kid or "")),
        )
        yield conn


def one(conn: Any, sql: str, params: tuple = ()) -> dict:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        raise HTTPException(404, "Record not found")
    return row
