import copy
from uuid import UUID, uuid4

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from habitquest_backend.catalog.models import ChildProfile, Habit, Level, Release
from habitquest_backend.catalog.repository import load_release
from habitquest_backend.catalog.revalidation import stale_references
from habitquest_backend.catalog.scoring import PRIMARY_STRENGTH
from habitquest_backend.db import one
from habitquest_backend.planning import entry


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
    # The new plan answers every flag on the plans it supersedes.
    conn.execute(
        "UPDATE hq.plan_flags SET resolved_at=now() WHERE resolved_at IS NULL AND plan_id IN "
        "(SELECT id FROM hq.plans WHERE child_id=%s AND id<>%s)",
        (proposal["child_id"], plan["id"]),
    )
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


def released(release: Release, habit_id: str, version: int) -> Habit:
    problems = stale_references(release, [(habit_id, version)], [])
    if problems:
        raise ValueError(f"stale: {problems[0]}; start a new plan run")
    return release.habits[habit_id]


def level_of(habit: Habit, name: str) -> Level:
    level = next((lv for lv in habit.levels if lv.level == name), None)
    if level is None:
        raise ValueError(f"level: {habit.id!r} has no {name!r} level")
    return level


def built_entry(habit: Habit, goal_ids: list[str], level: Level, profile: ChildProfile, **fields) -> dict:
    """An active entry rebuilt from the reviewed release, as the planner would have written it."""
    if habit.routine_anchor not in profile.routine_schedule:
        raise ValueError(
            f"needs_cue: add a {habit.routine_anchor} cue to the profile before adding {habit.id!r}"
        )
    return {**entry(habit, goal_ids, level, profile), **fields}


def strong_goals(habit: Habit, goal_ids: list[str]) -> list[str]:
    return [goal for goal in goal_ids if habit.goals.get(goal, 0) >= PRIMARY_STRENGTH]


def find(entries: list[dict], habit_id: str, op: str, where: str) -> dict:
    found = next((h for h in entries if h["habit_id"] == habit_id), None)
    if found is None:
        raise ValueError(f"{op}: {habit_id!r} is not {where}")
    return found


def edited(data: dict, edit: dict, release: Release, profile: ChildProfile, plan_version: int) -> dict:
    data = copy.deepcopy(data)
    op = edit["op"]
    active, future, goals = data["active"], data["future"], data["goals"]
    goal_ids = [g["goal_id"] for g in goals]
    if op == "remove_goal":
        data["goals"] = [g for g in goals if g["goal_id"] != edit["goal_id"]]
    elif op == "reorder_goals":
        by_id = {g["goal_id"]: g for g in goals}
        if sorted(edit["goal_ids"]) != sorted(by_id):
            raise ValueError("reorder_goals: goal_ids must list every proposal goal exactly once")
        data["goals"] = [by_id[gid] for gid in edit["goal_ids"]]
    elif op == "change_level":
        old = find(active, edit["habit_id"], op, "an active habit")
        habit = released(release, old["habit_id"], old["version"])
        rebuilt = built_entry(
            habit,
            old["goal_ids"],
            level_of(habit, edit["level"]),
            profile,
            cue=old["cue"],
            days=old["days"],
            covers=old["covers"],
            added_in_plan_version=old["added_in_plan_version"],
        )
        active[active.index(old)] = rebuilt
    elif op == "change_schedule":
        old = find(active, edit["habit_id"], op, "an active habit")
        old["cue"], old["days"] = edit["cue"], edit["days"]
    elif op == "swap_habit":
        old = find(active, edit["old_habit_id"], op, "an active habit")
        new_id = edit["new_habit_id"]
        served = [
            g for g in old["covers"] if any(o["habit_id"] == new_id for o in data["shortlists"].get(g, []))
        ]
        if not served:
            raise ValueError(f"swap_habit: {new_id!r} is not in the shortlist for goals {old['covers']}")
        if any(h["habit_id"] == new_id for h in active):
            raise ValueError(f"swap_habit: {new_id!r} is already active")
        current = release.habits.get(new_id)
        habit = released(release, new_id, current.version if current else 0)
        goals_served = [g for g in goal_ids if g in habit.goals]
        rebuilt = built_entry(
            habit,
            goals_served,
            level_of(habit, edit["level"]),
            profile,
            covers=sorted(set(served) | set(strong_goals(habit, goals_served))),
            added_in_plan_version=old["added_in_plan_version"],
        )
        active[active.index(old)] = rebuilt
        future[:] = [h for h in future if h["habit_id"] != new_id]
        future.append(
            {"habit_id": old["habit_id"], "version": old["version"], "goal_ids": old["goal_ids"], "score": 0}
        )
    elif op == "demote_active":
        old = find(active, edit["habit_id"], op, "an active habit")
        active.remove(old)
        future.append(
            {"habit_id": old["habit_id"], "version": old["version"], "goal_ids": old["goal_ids"], "score": 0}
        )
    elif op == "promote_future":
        candidate = find(future, edit["habit_id"], op, "a future candidate")
        habit = released(release, candidate["habit_id"], candidate["version"])
        rebuilt = built_entry(
            habit,
            candidate["goal_ids"],
            level_of(habit, edit["level"]),
            profile,
            covers=strong_goals(habit, [g for g in candidate["goal_ids"] if g in goal_ids]),
            added_in_plan_version=plan_version,
        )
        future.remove(candidate)
        active.append(rebuilt)
    else:
        raise ValueError(f"Unknown patch op: {op!r}")
    return data


def apply_patch(
    data: dict,
    edit: dict,
    release: Release,
    profile: ChildProfile,
    *,
    max_active: int,
    available_minutes: int,
    plan_version: int,
) -> dict:
    """Apply one parent edit and re-run the publication rules; raises ValueError (→ 422) naming the rule.
    Limits are the child's daily totals; `plan_version` is the version confirmation would create."""
    after = edited(data, edit, release, profile, plan_version)
    active = after["active"]
    if len(active) > max_active:
        raise ValueError(
            f"max_active_habits: a plan holds at most {max_active} active habits; demote one first"
        )
    minutes = sum(h["minutes"] for h in active)
    if minutes > available_minutes:
        raise ValueError(f"available_minutes: {minutes} minutes exceeds the daily {available_minutes}")
    # Strict coverage: a weak link covers a goal only while no cue-ready alternative for it is left
    # inactive, whether shortlisted or a future candidate (a demoted habit returns to future).
    active_ids = {h["habit_id"] for h in active}
    cue_ready = {
        f["habit_id"]
        for f in after["future"]
        if f["habit_id"] in release.habits
        and release.habits[f["habit_id"]].routine_anchor in profile.routine_schedule
    }

    def alternatives(goal: str) -> set[str]:
        listed = {o["habit_id"] for o in after["shortlists"].get(goal, [])}
        return listed | {
            f["habit_id"] for f in after["future"] if f["habit_id"] in cue_ready and goal in f["goal_ids"]
        }

    for h in active:
        habit = release.habits.get(h["habit_id"])
        h["covers"] = [
            goal
            for goal in h["covers"]
            if habit is None
            or habit.goals.get(goal, 0) >= PRIMARY_STRENGTH
            or alternatives(goal) <= active_ids
        ]
    covered = {goal for h in active for goal in h["covers"]}
    kept = {g["goal_id"] for g in after["goals"]}
    lost = [
        g["goal_id"] for g in data["goals"] if g["status"] == "covered" and g["goal_id"] in kept - covered
    ]
    if lost:
        raise ValueError(
            f"coverage: {lost} would no longer be covered; remove the goal or swap within it instead"
        )
    for goal in after["goals"]:
        if goal["goal_id"] in covered:
            goal["status"] = "covered"
    after["unresolved"] = {g: r for g, r in after["unresolved"].items() if g in kept and g not in covered}
    chapters = [c for c in after["chapters"] if c["goal_id"] in covered]
    for goal in after["goals"]:
        if goal["goal_id"] in covered and not any(c["goal_id"] == goal["goal_id"] for c in chapters):
            chapter = release.chapters.get((after["world"], goal["goal_id"]))
            if chapter is None:
                raise ValueError(f"chapter: no approved {after['world']} chapter for {goal['goal_id']!r}")
            chapters.append(
                {
                    "id": chapter.id,
                    "version": chapter.version,
                    "goal_id": chapter.goal_id,
                    "title": chapter.title,
                }
            )
    after["chapters"] = chapters
    return after


def flag_plan(conn, plan_id: UUID, kind: str, habit_id: str, reason: str) -> None:
    conn.execute(
        "INSERT INTO hq.plan_flags(plan_id,kind,habit_id,reason) VALUES(%s,%s,%s,%s)",
        (plan_id, kind, habit_id, reason),
    )


def cue_loss_habits(old_schedule: dict, new_schedule: dict, active: list[dict]) -> list[str]:
    """Return habit_ids whose routine_anchor cue is being removed by the profile update."""
    removed_anchors = {anchor for anchor in old_schedule if anchor not in new_schedule}
    return [h["habit_id"] for h in active if h.get("anchor") in removed_anchors]
