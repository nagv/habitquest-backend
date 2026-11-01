"""The daily loop: today's habits, kids submitting completions, caregivers approving them, Sparks and points."""

from datetime import date, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from habitquest_backend.accounts import StrictModel
from habitquest_backend.auth import KID, signed_in
from habitquest_backend.coach import save_suggestion
from habitquest_backend.config import Settings
from habitquest_backend.db import one, transaction
from habitquest_backend.journey import quest_stars
from habitquest_backend.onboarding import DAYS, MAX_ACTIVE_HABITS, progress

MAX_MULTIPLIER = 3
STREAK_DAYS = 3
STREAK_GRACE_DAYS = 2  # missed quest days in a row that pause a streak; the third ends it
WELCOME_BACK_DAYS = 3  # missed quest days before the first day back earns a bonus
LOOK_BACK_DAYS = 60


class Submission(StrictModel):
    child_habit_id: UUID


def balance(conn, child_id: UUID) -> int:
    """Spendable points: the sum of the child's ledger."""
    return conn.execute(
        "SELECT coalesce(sum(amount),0) AS n FROM hq.points_ledger WHERE child_id=%s", (child_id,)
    ).fetchone()["n"]


def golden_habit(habits: list[str], day: date, child_id: UUID) -> str:
    """FNV-1a, as the frontend used, so each child gets a fresh golden quest each day."""
    value = 2166136261
    for character in f"{day}:{child_id}":
        value = ((value ^ ord(character)) * 16777619) & 0xFFFFFFFF
    return habits[value % len(habits)]


def quest_days(conn, child_id: UUID) -> set[str]:
    """The days of the week the child has an active habit. Any other day is a rest day, which is never a miss."""
    rows = conn.execute(
        "SELECT DISTINCT unnest(days) AS day FROM hq.child_habits WHERE child_id=%s AND status='active'",
        (child_id,),
    ).fetchall()
    return {row["day"] for row in rows}


def is_quest_day(day: date, week: set[str]) -> bool:
    return DAYS[day.weekday()] in week


def quest_days_before(day: date, week: set[str]) -> list[date]:
    """Recent quest days, newest first."""
    earlier = (day - timedelta(n) for n in range(1, LOOK_BACK_DAYS))
    return [past for past in earlier if is_quest_day(past, week)]


def boosts(conn, child_id: UUID, day: date) -> dict[str, list[str]]:
    """Why each habit on `day` would earn extra: `golden`, `streak`, `double` or `welcome`, each worth 1x more."""
    habits = conn.execute(
        "SELECT habit_id FROM hq.child_habits WHERE child_id=%s AND status='active'"
        " AND to_char(%s::date,'dy')=ANY(days) ORDER BY habit_id",
        (child_id, day),
    ).fetchall()
    if not habits:
        return {}
    done = conn.execute(
        "SELECT DISTINCT c.day FROM hq.completions c JOIN hq.child_habits ch ON ch.id=c.child_habit_id"
        " WHERE ch.child_id=%s AND c.status='approved' AND c.day<%s",
        (child_id, day),
    ).fetchall()
    days = {row["day"] for row in done}
    earlier = quest_days_before(day, quest_days(conn, child_id))
    recent = earlier[: STREAK_DAYS - 1]
    shared = ["streak"] if len(recent) == STREAK_DAYS - 1 and all(past in days for past in recent) else []
    double = conn.execute(
        "SELECT 1 FROM hq.surprises WHERE kind='double' AND day=%s AND (child_id IS NULL OR child_id=%s)"
        " AND family_id=(SELECT family_id FROM hq.children WHERE id=%s)",
        (day, child_id, child_id),
    ).fetchone()
    shared += ["double"] if double else []
    last = max(days, default=None)
    missed = len([past for past in earlier if last and past > last])
    shared += ["welcome"] if missed >= WELCOME_BACK_DAYS else []
    golden = golden_habit([h["habit_id"] for h in habits], day, child_id)
    return {h["habit_id"]: (["golden"] if h["habit_id"] == golden else []) + shared for h in habits}


def multiplier(reasons: list[str]) -> int:
    return min(1 + len(reasons), MAX_MULTIPLIER)


def history_view(conn, child_id: UUID, day: date) -> dict:
    """The current streak, and approved habits per day for the last seven days. Rest days are skipped, and a
    quest day or two missed pauses the streak without ending it.
    """
    rows = conn.execute(
        "SELECT c.day,count(*) AS n FROM hq.completions c JOIN hq.child_habits ch ON ch.id=c.child_habit_id"
        " WHERE ch.child_id=%s AND c.status='approved' AND c.day>%s::date-60 GROUP BY c.day",
        (child_id, day),
    ).fetchall()
    counts = {row["day"]: row["n"] for row in rows}
    cursor = day if day in counts else day - timedelta(1)
    streak = missed = 0
    quest_week = quest_days(conn, child_id)
    while missed <= STREAK_GRACE_DAYS and cursor > day - timedelta(LOOK_BACK_DAYS):
        if cursor in counts:
            streak += 1
            missed = 0
        elif is_quest_day(cursor, quest_week):
            missed += 1
        cursor -= timedelta(1)
    week = {(day - timedelta(n)).isoformat(): counts.get(day - timedelta(n), 0) for n in range(6, -1, -1)}
    return {"streak": streak, "week": week}


def today_view(conn, child_id: UUID) -> dict:
    """The child's habits scheduled for today (in the family's time zone), each with today's status."""
    child = one(conn, "SELECT id,name,buddy,world FROM hq.children WHERE id=%s", (child_id,))
    day = conn.execute("SELECT hq.child_today(%s) AS day", (child_id,)).fetchone()["day"]
    habits = conn.execute(
        "SELECT ch.id AS child_habit_id,h.id AS habit_id,h.kid_title,h.kid_hint,h.icon,h.anchor,h.tier,h.points,"
        "to_char(ch.cue_time,'HH24:MI') AS cue_time,coalesce(c.status,'todo') AS status,c.id AS completion_id"
        " FROM hq.child_habits ch JOIN hq.habits h ON h.id=ch.habit_id"
        " LEFT JOIN hq.completions c ON c.child_habit_id=ch.id AND c.day=%s"
        " WHERE ch.child_id=%s AND ch.status='active' AND to_char(%s::date,'dy')=ANY(ch.days)"
        " ORDER BY ch.cue_time,h.kid_title",
        (day, child_id, day),
    ).fetchall()
    reasons = boosts(conn, child_id, day)
    stars = {row["habit_id"]: row["stars"] for row in quest_stars(conn, child_id)}
    for habit in habits:
        habit["boosts"] = reasons.get(habit["habit_id"], [])
        habit["multiplier"] = multiplier(habit["boosts"])
        habit["stars"] = stars.get(habit["habit_id"], 0)
    week = quest_days(conn, child_id)
    upcoming = (day + timedelta(n) for n in range(1, 8))
    return {
        "child": child,
        "day": day,
        "welcome_back": any("welcome" in habit["boosts"] for habit in habits),
        "rest_day": bool(week) and not habits,
        "next_quest_day": next((d for d in upcoming if is_quest_day(d, week)), None) if not habits else None,
        "progress": progress(conn, child_id),
        "points": balance(conn, child_id),
        "habits": habits,
        **history_view(conn, child_id, day),
    }


def suggest_after_mastery(conn, child_id: UUID, habit_id: str) -> None:
    """When a quest reaches gold, offers the parent a harder quest for the same goal, or retiring this one."""
    child = one(conn, "SELECT age FROM hq.children WHERE id=%s", (child_id,))
    habit = one(conn, "SELECT goal_id,tier,title FROM hq.habits WHERE id=%s", (habit_id,))
    planned = conn.execute(
        "SELECT habit_id FROM hq.child_habits WHERE child_id=%s AND status<>'removed'", (child_id,)
    ).fetchall()
    harder = conn.execute(
        "SELECT id,title FROM hq.habits WHERE is_active AND goal_id=%s AND tier>%s AND tier<=%s"
        " AND %s BETWEEN min_age AND max_age AND NOT id=ANY(%s) ORDER BY tier,title LIMIT 1",
        (
            habit["goal_id"],
            habit["tier"],
            progress(conn, child_id)["unlocked_tier"],
            child["age"],
            [row["habit_id"] for row in planned],
        ),
    ).fetchone()
    if harder and len(planned) < MAX_ACTIVE_HABITS:
        reason = f"{habit['title']} is mastered, so this could be a good next step."
        save_suggestion(conn, child_id, "mastery", "add", harder["id"], None, reason)
    else:
        reason = (
            "This quest is mastered. If it's now part of everyday life, retiring it makes room for a new one."
        )
        save_suggestion(conn, child_id, "mastery", "remove", habit_id, None, reason)


def router(settings: Settings) -> APIRouter:
    api = APIRouter()
    User = Annotated[UUID, Depends(signed_in(settings))]
    Kid = Annotated[UUID, Depends(signed_in(settings, KID))]

    def decide(user: UUID, completion_id: UUID, status: str) -> dict:
        with transaction(settings, user) as conn:
            row = one(
                conn,
                "SELECT c.status,c.day,ch.child_id,ch.habit_id,h.points FROM hq.completions c"
                " JOIN hq.child_habits ch ON ch.id=c.child_habit_id JOIN hq.habits h ON h.id=ch.habit_id"
                " WHERE c.id=%s FOR UPDATE OF c",
                (completion_id,),
            )
            if row["status"] != "submitted":
                raise HTTPException(409, f"This completion was already {row['status']}")
            completion = one(
                conn,
                "UPDATE hq.completions SET status=%s,decided_by=%s,decided_at=now() WHERE id=%s"
                " RETURNING id,status,day,submitted_at,decided_at",
                (status, user, completion_id),
            )
            if status == "approved":
                reasons = boosts(conn, row["child_id"], row["day"]).get(row["habit_id"], [])
                conn.execute(
                    "INSERT INTO hq.points_ledger(child_id,amount,reason,completion_id,created_by)"
                    " VALUES(%s,%s,'earned',%s,%s)",
                    (row["child_id"], row["points"] * multiplier(reasons), completion_id, user),
                )
                conn.execute(
                    "UPDATE hq.children SET world=%s WHERE id=%s",
                    (progress(conn, row["child_id"])["world"], row["child_id"]),
                )
                done = next(q for q in quest_stars(conn, row["child_id"]) if q["habit_id"] == row["habit_id"])
                if done["approvals"] == done["targets"][-1]:
                    suggest_after_mastery(conn, row["child_id"], row["habit_id"])
            return {
                "completion": completion,
                "child_id": row["child_id"],
                "progress": progress(conn, row["child_id"]),
                "points": balance(conn, row["child_id"]),
            }

    @api.get("/v1/kid/today", tags=["Kid mode"], summary="Today's quests")
    def kid_today(kid: Kid) -> dict:
        """With a kid token: today's date in the family's time zone, the child's Sparks, level and points, and
        the habits scheduled for today with kid-facing wording, icon, time, points and `status`: `todo`,
        `submitted` (waiting for a grown-up), `approved` or `rejected` (try again).
        """
        with transaction(settings, kid=kid) as conn:
            return today_view(conn, kid)

    @api.post("/v1/kid/completions", tags=["Kid mode"], summary="I did it!")
    def kid_submit(body: Submission, kid: Kid) -> dict:
        """With a kid token: marks one of today's habits as done, for a caregiver to approve. A `rejected` habit
        can be submitted again; submitting one already `submitted` changes nothing. 404 when the habit is not
        one of the child's habits for today, 409 when it is already approved. Returns today's view.
        """
        with transaction(settings, kid=kid) as conn:
            view = today_view(conn, kid)
            entry = next((h for h in view["habits"] if h["child_habit_id"] == body.child_habit_id), None)
            if entry is None:
                raise HTTPException(404, "That is not one of today's habits")
            if entry["status"] == "approved":
                raise HTTPException(409, "Already approved today")
            if entry["status"] == "rejected":
                conn.execute(
                    "UPDATE hq.completions SET status='submitted',submitted_at=now(),decided_by=NULL,decided_at=NULL"
                    " WHERE id=%s",
                    (entry["completion_id"],),
                )
            else:
                conn.execute(
                    "INSERT INTO hq.completions(child_habit_id,day) VALUES(%s,%s)"
                    " ON CONFLICT (child_habit_id,day) DO NOTHING",
                    (body.child_habit_id, view["day"]),
                )
            return today_view(conn, kid)

    @api.get("/v1/children/{child_id}/today", tags=["Daily"], summary="A child's day")
    def child_today(child_id: UUID, user: User) -> dict:
        """The same view the child sees in kid mode: today's habits with their status, Sparks, level and points.
        404 unless the caller cares for the child.
        """
        with transaction(settings, user) as conn:
            return today_view(conn, child_id)

    @api.get("/v1/families/{family_id}/approvals", tags=["Daily"], summary="Waiting for approval")
    def approvals(family_id: UUID, user: User) -> list[dict]:
        """Every submitted completion in the family waiting for a caregiver, oldest first: the child, the habit
        (parent and kid wording, icon, points) and the day. Earlier days stay here until decided.
        """
        with transaction(settings, user) as conn:
            one(conn, "SELECT id FROM hq.families WHERE id=%s", (family_id,))
            return conn.execute(
                "SELECT c.id AS completion_id,k.id AS child_id,k.name AS child_name,h.id AS habit_id,h.title,"
                "h.kid_title,h.icon,h.points,c.day,c.submitted_at FROM hq.completions c"
                " JOIN hq.child_habits ch ON ch.id=c.child_habit_id JOIN hq.children k ON k.id=ch.child_id"
                " JOIN hq.habits h ON h.id=ch.habit_id WHERE k.family_id=%s AND c.status='submitted'"
                " ORDER BY c.submitted_at,c.id",
                (family_id,),
            ).fetchall()

    @api.post("/v1/completions/{completion_id}/approve", tags=["Daily"], summary="Approve")
    def approve(completion_id: UUID, user: User) -> dict:
        """Approves a submitted completion: the child earns Sparks equal to the habit's tier, up to the daily
        cap, and the habit's points (added to the points ledger), once. Reaching gold on a quest also creates a
        suggestion for the parent. Returns the completion and the child's new `progress` and `points`; compare the
        level with the previous one to celebrate a level-up. 409 when it was already approved or rejected.
        """
        return decide(user, completion_id, "approved")

    @api.post("/v1/completions/{completion_id}/reject", tags=["Daily"], summary="Not yet")
    def reject(completion_id: UUID, user: User) -> dict:
        """Marks a submitted completion "not yet": no Spark or points, and the child can submit it again today.
        409 when it was already approved or rejected.
        """
        return decide(user, completion_id, "rejected")

    return api
