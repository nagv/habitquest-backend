from uuid import UUID, uuid4

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from habitquest_backend.db import one

GOALS = {
    "bedtime": "Build a consistent bedtime preparation routine",
    "morning": "Be ready for the day with fewer reminders",
    "meals": "Join the after-school meal routine",
    "responsibility": "Care for belongings and shared spaces",
}


def questions(data: dict) -> list[str]:
    result = []
    if not data["goals"]:
        result.append("Which routines should we focus on: bedtime, morning, meals or responsibility?")
    for goal in ["bedtime", "morning"]:
        if goal in data["goals"] and not data.get(goal):
            result.append(f"What {goal} readiness time have you agreed? Use HH:MM.")
    return result


def session_view(row: dict) -> dict:
    return {**row, "questions": questions(row["data"]), "goal_titles": GOALS}


def eligible(conn, child: dict, data: dict) -> list[dict]:
    low, high = map(int, child["age_band"].split("-"))
    return conn.execute(
        "SELECT * FROM hq.catalog WHERE approved AND min_age<=%s AND max_age>=%s "
        "AND minutes<=%s AND category=ANY(%s) ORDER BY id",
        (low, high, data["available_minutes"], data["goals"]),
    ).fetchall()


def current_version(conn, child_id: UUID) -> int:
    return conn.execute(
        "SELECT coalesce(max(version),0) AS version FROM hq.plans WHERE child_id=%s", (child_id,)
    ).fetchone()["version"]


def enqueue(conn, session_id: UUID, parent: UUID, key: str, kind: str = "plan") -> dict:
    row = one(conn, "SELECT * FROM hq.sessions WHERE id=%s FOR UPDATE", (session_id,))
    if kind == "plan" and (not row["goals_confirmed"] or questions(row["data"])):
        raise HTTPException(409, "Confirm goals and answer clarification questions first")
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (str(parent),))
    old = conn.execute(
        "SELECT * FROM hq.jobs WHERE parent_id=%s AND request_key=%s", (parent, key)
    ).fetchone()
    if old:
        if old["session_id"] != session_id or old["session_version"] != row["version"] or old["kind"] != kind:
            raise HTTPException(409, "Idempotency key was used for another request version")
        return old
    return one(
        conn,
        "INSERT INTO hq.jobs(id,session_id,child_id,parent_id,session_version,base_plan_version,request_key,kind) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
        (
            uuid4(),
            session_id,
            row["child_id"],
            parent,
            row["version"],
            current_version(conn, row["child_id"]),
            key,
            kind,
        ),
    )


def validate_sources(conn, data: dict) -> None:
    conn.execute(
        "SELECT hq.lock_sources(%s,%s)",
        ([r["id"] for r in data["habits"]], [r["id"] for r in data["chapters"]]),
    )
    policy = one(conn, "SELECT * FROM hq.policy WHERE name='onboarding'")
    if data["policy_version"] != policy["version"]:
        raise HTTPException(409, "Policy changed; request a new reviewed proposal")
    for item in data["habits"]:
        source = conn.execute(
            "SELECT * FROM hq.catalog WHERE id=%s AND approved AND version=%s", (item["id"], item["version"])
        ).fetchone()
        if not source or source != item:
            raise HTTPException(409, "Catalog changed; request a new reviewed proposal")
    for chapter in data["chapters"]:
        source = conn.execute(
            "SELECT * FROM hq.chapters WHERE id=%s AND approved AND version=%s",
            (chapter["id"], chapter["version"]),
        ).fetchone()
        if not source or source != chapter:
            raise HTTPException(409, "Chapter content changed; request a new proposal")


def confirm(conn, proposal_id: UUID, expected: int) -> dict:
    proposal = one(
        conn, "SELECT *,expires_at<=now() AS expired FROM hq.proposals WHERE id=%s FOR UPDATE", (proposal_id,)
    )
    previous = conn.execute("SELECT * FROM hq.plans WHERE proposal_id=%s", (proposal_id,)).fetchone()
    if previous:
        return previous
    one(conn, "SELECT * FROM hq.children WHERE id=%s", (proposal["child_id"],))
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,1))", (str(proposal["child_id"]),))
    session = one(
        conn,
        "SELECT * FROM hq.sessions WHERE id=(SELECT session_id FROM hq.jobs WHERE id=%s) FOR UPDATE",
        (proposal_id,),
    )
    if proposal["expired"] or session["version"] != proposal["session_version"]:
        raise HTTPException(409, "Proposal is expired or onboarding has changed")
    if expected != proposal["base_plan_version"] or current_version(conn, proposal["child_id"]) != expected:
        raise HTTPException(409, "Current plan version changed")
    validate_sources(conn, proposal["data"])
    plan = one(
        conn,
        "INSERT INTO hq.plans(id,child_id,parent_id,proposal_id,version,data) VALUES(%s,%s,%s,%s,%s,%s) RETURNING *",
        (
            uuid4(),
            proposal["child_id"],
            proposal["parent_id"],
            proposal_id,
            expected + 1,
            Jsonb(proposal["data"]),
        ),
    )
    conn.execute("UPDATE hq.proposals SET confirmed=true WHERE id=%s", (proposal_id,))
    for kind, value in [
        ("world", session["data"]["world"]),
        ("routine_times", {k: session["data"][k] for k in ["bedtime", "morning"] if session["data"][k]}),
    ]:
        conn.execute(
            "INSERT INTO hq.preferences(id,child_id,parent_id,kind,value,source_id) VALUES(%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(child_id,kind) DO UPDATE SET value=excluded.value,source_id=excluded.source_id,approved_at=now()",
            (uuid4(), proposal["child_id"], proposal["parent_id"], kind, Jsonb(value), plan["id"]),
        )
    return plan
