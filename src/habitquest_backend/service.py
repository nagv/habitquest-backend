from uuid import UUID, uuid4

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from habitquest_backend.catalog.repository import load_release
from habitquest_backend.catalog.revalidation import stale_references
from habitquest_backend.db import one


def current_version(conn, child_id: UUID) -> int:
    return conn.execute(
        "SELECT coalesce(max(version),0) AS version FROM hq.plans WHERE child_id=%s", (child_id,)
    ).fetchone()["version"]


def enqueue(conn, intake_id: UUID, parent: UUID, key: str) -> dict:
    row = one(conn, "SELECT * FROM hq.intakes WHERE id=%s FOR UPDATE", (intake_id,))
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (str(parent),))
    old = conn.execute(
        "SELECT * FROM hq.jobs WHERE parent_id=%s AND request_key=%s", (parent, key)
    ).fetchone()
    if old:
        if old["intake_id"] != intake_id or old["intake_version"] != row["version"]:
            raise HTTPException(409, "Idempotency key was used for another request version")
        return old
    return one(
        conn,
        "INSERT INTO hq.jobs(id,intake_id,child_id,parent_id,intake_version,base_plan_version,request_key) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *",
        (
            uuid4(),
            intake_id,
            row["child_id"],
            parent,
            row["version"],
            current_version(conn, row["child_id"]),
            key,
        ),
    )


def validate_sources(conn, data: dict) -> int:
    """Lock and revalidate the proposal's active habits and chapters against the current release;
    return the id of the release that was checked."""
    habits = [(h["habit_id"], h["version"]) for h in data["active"]]
    conn.execute("SELECT hq.lock_release(%s)", ([habit_id for habit_id, _ in habits],))
    policy = one(conn, "SELECT * FROM hq.policy WHERE name='onboarding'")
    if data["policy_version"] != policy["version"]:
        raise HTTPException(409, "Policy changed; request a new reviewed proposal")
    chapters = [(c["id"], c["version"]) for c in data["chapters"]]
    release = load_release(conn)
    problems = stale_references(release, habits, chapters)
    if problems:
        raise HTTPException(409, "replan_required: " + "; ".join(problems))
    return release.id


def confirm(conn, proposal_id: UUID, expected_plan: int, expected_proposal: int) -> dict:
    proposal = one(
        conn, "SELECT *,expires_at<=now() AS expired FROM hq.proposals WHERE id=%s FOR UPDATE", (proposal_id,)
    )
    previous = conn.execute("SELECT * FROM hq.plans WHERE proposal_id=%s", (proposal_id,)).fetchone()
    if previous:
        return previous
    child = one(conn, "SELECT profile_version FROM hq.children WHERE id=%s", (proposal["child_id"],))
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,1))", (str(proposal["child_id"]),))
    job = one(conn, "SELECT intake_id FROM hq.jobs WHERE id=%s", (proposal_id,))
    intake = one(conn, "SELECT * FROM hq.intakes WHERE id=%s FOR UPDATE", (job["intake_id"],))
    if (
        proposal["expired"]
        or intake["version"] != proposal["intake_version"]
        or proposal["version"] != expected_proposal
    ):
        raise HTTPException(409, "Proposal is expired, edited, or its intake has changed")
    if (
        expected_plan != proposal["base_plan_version"]
        or current_version(conn, proposal["child_id"]) != expected_plan
    ):
        raise HTTPException(409, "Current plan version changed")
    if proposal["data"].get("profile_version") != child["profile_version"]:
        raise HTTPException(409, "Profile changed; request a new proposal")
    release_id = validate_sources(conn, proposal["data"])
    plan = one(
        conn,
        "INSERT INTO hq.plans(id,child_id,parent_id,proposal_id,version,data,release_id) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *",
        (
            uuid4(),
            proposal["child_id"],
            proposal["parent_id"],
            proposal_id,
            expected_plan + 1,
            Jsonb(proposal["data"]),
            release_id,
        ),
    )
    conn.execute("UPDATE hq.proposals SET confirmed=true WHERE id=%s", (proposal_id,))
    for goal in proposal["data"]["goals"]:
        if goal["status"] == "covered":
            conn.execute(
                "INSERT INTO hq.child_goals(child_id,parent_id,goal_id,status,since_plan_version,source_intake_id) "
                "VALUES(%s,%s,%s,'active',%s,%s) ON CONFLICT(child_id,goal_id) DO UPDATE SET status='active',"
                "since_plan_version=EXCLUDED.since_plan_version,source_intake_id=EXCLUDED.source_intake_id "
                "WHERE hq.child_goals.status<>'active'",
                (proposal["child_id"], proposal["parent_id"], goal["goal_id"], plan["version"], intake["id"]),
            )
    return plan
