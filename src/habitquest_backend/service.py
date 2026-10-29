import copy
from uuid import UUID, uuid4

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from habitquest_backend.catalog.models import Release
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


def apply_patch(data: dict, edit: dict, release: Release) -> dict:
    """Pure dict transform; raises ValueError (→ 422) for invalid edits."""
    data = copy.deepcopy(data)
    op = edit["op"]
    active = data["active"]
    future = data["future"]
    goals = data["goals"]
    shortlists = data.get("shortlists", {})

    if op == "remove_goal":
        gid = edit["goal_id"]
        data["goals"] = [g for g in goals if g["goal_id"] != gid]
        return data

    if op == "reorder_goals":
        ordered = edit["goal_ids"]
        by_id = {g["goal_id"]: g for g in goals}
        if sorted(ordered) != sorted(by_id):
            raise ValueError("reorder_goals: goal_ids must list every proposal goal exactly once")
        data["goals"] = [by_id[gid] for gid in ordered]
        return data

    if op == "change_level":
        hid, new_level = edit["habit_id"], edit["level"]
        entry = next((h for h in active if h["habit_id"] == hid), None)
        if entry is None:
            raise ValueError(f"change_level: {hid!r} is not an active habit")
        entry["level"] = new_level
        return data

    if op == "change_schedule":
        hid = edit["habit_id"]
        entry = next((h for h in active if h["habit_id"] == hid), None)
        if entry is None:
            raise ValueError(f"change_schedule: {hid!r} is not an active habit")
        entry["cue"] = edit["cue"]
        entry["days"] = edit["days"]
        return data

    if op == "swap_habit":
        old_id, new_id, new_level = edit["old_habit_id"], edit["new_habit_id"], edit["level"]
        old = next((h for h in active if h["habit_id"] == old_id), None)
        if old is None:
            raise ValueError(f"swap_habit: {old_id!r} is not active")
        goal_ids = old["goal_ids"]
        in_shortlist = any(o["habit_id"] == new_id for g in goal_ids for o in shortlists.get(g, []))
        if not in_shortlist:
            raise ValueError(f"swap_habit: {new_id!r} is not in the shortlist for goals {goal_ids}")
        idx = next(i for i, h in enumerate(active) if h["habit_id"] == old_id)
        active[idx] = {**old, "habit_id": new_id, "level": new_level}
        return data

    if op == "demote_active":
        hid = edit["habit_id"]
        entry = next((h for h in active if h["habit_id"] == hid), None)
        if entry is None:
            raise ValueError(f"demote_active: {hid!r} is not an active habit")
        data["active"] = [h for h in active if h["habit_id"] != hid]
        data["future"].append(
            {
                "habit_id": hid,
                "version": entry.get("version", 1),
                "goal_ids": entry["goal_ids"],
                "score": 0,
            }
        )
        return data

    if op == "promote_future":
        hid, new_level = edit["habit_id"], edit["level"]
        entry = next((h for h in future if h["habit_id"] == hid), None)
        if entry is None:
            raise ValueError(f"promote_future: {hid!r} is not in future candidates")
        problems = stale_references(release, [(hid, entry["version"])], [])
        if problems:
            raise ValueError(f"promote_future: candidate is stale ({problems[0]})")
        level_obj = next((lv for lv in release.habits[hid].levels if lv.level == new_level), None)
        if level_obj is None:
            raise ValueError(f"promote_future: {hid!r} has no {new_level!r} level")
        data["future"] = [h for h in future if h["habit_id"] != hid]
        data["active"].append(
            {
                "habit_id": hid,
                "version": entry["version"],
                "goal_ids": entry["goal_ids"],
                "level": new_level,
                "minutes": level_obj.minutes,
                "covers": entry["goal_ids"],
                "cue": "",
                "days": [],
            }
        )
        return data

    raise ValueError(f"Unknown patch op: {op!r}")


def flag_plan(conn, plan_id: UUID, kind: str, habit_id: str, reason: str) -> None:
    conn.execute(
        "INSERT INTO hq.plan_flags(plan_id,kind,habit_id,reason) VALUES(%s,%s,%s,%s)",
        (plan_id, kind, habit_id, reason),
    )


def cue_loss_habits(old_schedule: dict, new_schedule: dict, active: list[dict]) -> list[str]:
    """Return habit_ids whose routine_anchor cue is being removed by the profile update."""
    removed_anchors = {anchor for anchor in old_schedule if anchor not in new_schedule}
    return [h["habit_id"] for h in active if h.get("anchor") in removed_anchors]
