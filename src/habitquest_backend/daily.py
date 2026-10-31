"""The daily loop: today's habits, kids submitting completions, caregivers approving them, Sparks and points."""

from datetime import date, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from habitquest_backend.accounts import StrictModel
from habitquest_backend.auth import KID, signed_in
from habitquest_backend.config import Settings
from habitquest_backend.db import one, transaction
from habitquest_backend.onboarding import progress

MAX_MULTIPLIER = 3
STREAK_DAYS = 3


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


def boosts(conn, child_id: UUID, day: date) -> dict[str, list[str]]:
    """Why each habit scheduled on `day` would earn extra: `golden`, `streak` or `double`, each worth 1x more."""
    habits = conn.execute(
        "SELECT habit_id FROM hq.child_habits WHERE child_id=%s AND status='active'"
        " AND to_char(%s::date,'dy')=ANY(days) ORDER BY habit_id",
        (child_id, day),
    ).fetchall()
    if not habits:
        return {}
    done = conn.execute(
        "SELECT DISTINCT c.day FROM hq.completions c JOIN hq.child_habits ch ON ch.id=c.child_habit_id"
        " WHERE ch.child_id=%s AND c.status='approved' AND c.day<%s AND c.day>=%s::date-%s",
        (child_id, day, day, STREAK_DAYS),
    ).fetchall()
    days = {row["day"] for row in done}
    shared = ["streak"] if all(day - timedelta(n) in days for n in range(1, STREAK_DAYS)) else []
    double = conn.execute(
        "SELECT 1 FROM hq.surprises WHERE kind='double' AND day=%s AND (child_id IS NULL OR child_id=%s)"
        " AND family_id=(SELECT family_id FROM hq.children WHERE id=%s)",
        (day, child_id, child_id),
    ).fetchone()
    shared += ["double"] if double else []
    golden = golden_habit([h["habit_id"] for h in habits], day, child_id)
    return {h["habit_id"]: (["golden"] if h["habit_id"] == golden else []) + shared for h in habits}


def multiplier(reasons: list[str]) -> int:
    return min(1 + len(reasons), MAX_MULTIPLIER)


def history_view(conn, child_id: UUID, day: date) -> dict:
    """The current streak, ending today or yesterday, and approved habits per day for the last seven days."""
    rows = conn.execute(
        "SELECT c.day,count(*) AS n FROM hq.completions c JOIN hq.child_habits ch ON ch.id=c.child_habit_id"
        " WHERE ch.child_id=%s AND c.status='approved' AND c.day>%s::date-60 GROUP BY c.day",
        (child_id, day),
    ).fetchall()
    counts = {row["day"]: row["n"] for row in rows}
    cursor = day if day in counts else day - timedelta(1)
    streak = 0
    while cursor in counts:
        streak += 1
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
    for habit in habits:
        habit["boosts"] = reasons.get(habit["habit_id"], [])
        habit["multiplier"] = multiplier(habit["boosts"])
    return {
        "child": child,
        "day": day,
        "progress": progress(conn, child_id),
        "points": balance(conn, child_id),
        "habits": habits,
        **history_view(conn, child_id, day),
    }


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
        """Approves a submitted completion: the child earns one Spark and the habit's points (added to the
        points ledger), once. Returns the completion and the child's new `progress` and `points`; compare the
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
