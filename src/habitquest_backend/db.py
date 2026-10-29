from contextlib import contextmanager
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from habitquest_backend.config import Settings


@contextmanager
def transaction(settings: Settings, parent: UUID | None = None, *, worker: bool = False):
    with psycopg.connect(settings.database_url, row_factory=dict_row, connect_timeout=3) as conn:
        conn.execute("SET LOCAL ROLE hq_worker" if worker else "SET LOCAL ROLE hq_api")
        conn.execute("SET LOCAL statement_timeout='10s'")
        conn.execute("SELECT set_config('app.parent_id',%s,true)", (str(parent or ""),))
        yield conn


def one(conn: Any, sql: str, params: tuple = ()) -> dict:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        from fastapi import HTTPException

        raise HTTPException(404, "Record not found")
    return row
