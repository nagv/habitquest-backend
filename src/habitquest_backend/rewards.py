"""Spending points: caregivers record what points were used for, and everyone sees the points history."""

import random
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field

from habitquest_backend.accounts import StrictModel
from habitquest_backend.auth import KID, check_pin, signed_in
from habitquest_backend.config import Settings
from habitquest_backend.daily import balance
from habitquest_backend.db import one, transaction
from habitquest_backend.extras import add_to_day

REWARD_COLUMNS = "id,title,emoji,cost,kind,minutes"
REQUEST_COLUMNS = "id,child_id,title,emoji,cost,minutes,mystery,status,day,created_at"
# A jackpot pays straight away; other prizes need a grown-up.
MYSTERY_PRIZES: list[dict] = [
    {"title": "Extra bedtime story", "emoji": "📖", "weight": 3},
    {"title": "Dance party with a grown-up", "emoji": "💃", "weight": 3},
    {"title": "Build a blanket fort", "emoji": "🏰", "weight": 2},
    {"title": "Choose tomorrow's breakfast", "emoji": "🥞", "weight": 3},
    {"title": "+10 minutes screen time", "emoji": "📺", "minutes": 10, "weight": 2},
    {"title": "Jackpot! 100 points", "emoji": "💰", "jackpot": 100, "weight": 1},
]
Limit = Annotated[int, Query(ge=1, le=200, description="How many entries, newest first")]
ENTRY_COLUMNS = (
    "l.id,l.amount,l.reason,l.note,l.created_at,h.id AS habit_id,h.kid_title,h.icon,c.day AS completion_day"
)
ENTRY_JOINS = (
    " FROM hq.points_ledger l LEFT JOIN hq.completions c ON c.id=l.completion_id"
    " LEFT JOIN hq.child_habits ch ON ch.id=c.child_habit_id LEFT JOIN hq.habits h ON h.id=ch.habit_id"
)


class RewardInput(StrictModel):
    title: str = Field(min_length=1, max_length=80)
    emoji: str = Field(min_length=1, max_length=8)
    cost: int = Field(gt=0, le=100_000)
    kind: Literal["screen", "privilege", "outing", "toy", "mystery"]
    minutes: int | None = Field(default=None, gt=0, le=240)


class RewardUpdate(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=80)
    emoji: str | None = Field(default=None, min_length=1, max_length=8)
    cost: int | None = Field(default=None, gt=0, le=100_000)


class RewardRequest(StrictModel):
    reward_id: UUID


class PinConfirm(StrictModel):
    pin: str = Field(pattern=r"^\d{4}$")


class Redemption(StrictModel):
    points: int = Field(gt=0, le=100_000, description="How many points to spend")
    note: str = Field(
        min_length=1, max_length=200, description='What they were used for, e.g. "30 min tablet"'
    )


def history(conn, child_id: UUID, limit: int, *, caregiver: bool) -> dict:
    """The balance, lifetime totals and the newest ledger entries. Caregivers also see who made each entry."""
    by = ",cg.display_name AS by" if caregiver else ""
    caregivers = (
        " LEFT JOIN hq.children k ON k.id=l.child_id"
        " LEFT JOIN hq.caregivers cg ON cg.user_id=l.created_by AND cg.family_id=k.family_id"
        if caregiver
        else ""
    )
    totals = conn.execute(
        "SELECT coalesce(sum(amount) FILTER (WHERE amount>0),0) AS earned,"
        "coalesce(-sum(amount) FILTER (WHERE amount<0),0) AS spent FROM hq.points_ledger WHERE child_id=%s",
        (child_id,),
    ).fetchone()
    entries = conn.execute(
        f"SELECT {ENTRY_COLUMNS}{by}{ENTRY_JOINS}{caregivers} WHERE l.child_id=%s ORDER BY l.id DESC LIMIT %s",
        (child_id, limit),
    ).fetchall()
    return {"points": balance(conn, child_id), **totals, "entries": entries}


def router(settings: Settings) -> APIRouter:
    api = APIRouter()
    User = Annotated[UUID, Depends(signed_in(settings))]
    Kid = Annotated[UUID, Depends(signed_in(settings, KID))]

    @api.post("/v1/children/{child_id}/redemptions", status_code=201, tags=["Points"], summary="Spend points")
    def redeem(child_id: UUID, body: Redemption, user: User) -> dict:
        """A caregiver records points the child has spent and what on (`note`), e.g. 50 points for
        "30 minutes of tablet". The points are deducted from the balance; 422 when the child does not have
        enough. Sparks are never spent. Returns the new entry and the remaining `points`.
        """
        with transaction(settings, user) as conn:
            # Locking the child serialises redemptions; the database also refuses a negative balance.
            one(conn, "SELECT id FROM hq.children WHERE id=%s FOR UPDATE", (child_id,))
            available = balance(conn, child_id)
            if body.points > available:
                raise HTTPException(422, f"Not enough points: {available} available")
            entry = one(
                conn,
                "INSERT INTO hq.points_ledger(child_id,amount,reason,note,created_by)"
                " VALUES(%s,%s,'redeemed',%s,%s) RETURNING id,amount,reason,note,created_at",
                (child_id, -body.points, body.note, user),
            )
            return {"entry": entry, "points": available - body.points}

    @api.get("/v1/children/{child_id}/points", tags=["Points"], summary="Points history")
    def points(child_id: UUID, user: User, limit: Limit = 50) -> dict:
        """The child's spendable `points`, lifetime `earned` and `spent`, and the newest `entries`: each
        approved habit (`earned`, with the habit and its day) and each redemption (`redeemed`, with its note),
        with the caregiver who made it (`by`).
        """
        with transaction(settings, user) as conn:
            one(conn, "SELECT id FROM hq.children WHERE id=%s", (child_id,))
            return history(conn, child_id, limit, caregiver=True)

    @api.get("/v1/kid/points", tags=["Kid mode"], summary="My points")
    def kid_points(kid: Kid, limit: Limit = 50) -> dict:
        """With a kid token: the child's own points, lifetime totals and newest entries, for the kid's reward
        screen.
        """
        with transaction(settings, kid=kid) as conn:
            return history(conn, kid, limit, caregiver=False)

    @api.get("/v1/families/{family_id}/rewards", tags=["Points"], summary="The reward shop")
    def rewards(family_id: UUID, user: User) -> list[dict]:
        """The family's active rewards, cheapest first. New families start with a default set."""
        with transaction(settings, user) as conn:
            one(conn, "SELECT id FROM hq.families WHERE id=%s", (family_id,))
            return conn.execute(
                f"SELECT {REWARD_COLUMNS} FROM hq.rewards WHERE family_id=%s AND is_active ORDER BY cost,title",
                (family_id,),
            ).fetchall()

    @api.post("/v1/families/{family_id}/rewards", status_code=201, tags=["Points"], summary="Add a reward")
    def add_reward(family_id: UUID, body: RewardInput, user: User) -> dict:
        """Adds a reward to the shop: `title`, `emoji`, `cost` in points, `kind` (screen, privilege, outing, toy,
        mystery), and `minutes` for screen-time rewards.
        """
        with transaction(settings, user) as conn:
            one(conn, "SELECT id FROM hq.families WHERE id=%s", (family_id,))
            return one(
                conn,
                "INSERT INTO hq.rewards(family_id,title,emoji,cost,kind,minutes) VALUES(%s,%s,%s,%s,%s,%s)"
                f" RETURNING {REWARD_COLUMNS}",
                (family_id, body.title, body.emoji, body.cost, body.kind, body.minutes),
            )

    @api.patch("/v1/rewards/{reward_id}", tags=["Points"], summary="Edit a reward")
    def edit_reward(reward_id: UUID, body: RewardUpdate, user: User) -> dict:
        """Changes a reward's `title`, `emoji` or `cost`. Requests already made keep the old values."""
        with transaction(settings, user) as conn:
            return one(
                conn,
                "UPDATE hq.rewards SET title=coalesce(%s,title),emoji=coalesce(%s,emoji),cost=coalesce(%s,cost)"
                f" WHERE id=%s RETURNING {REWARD_COLUMNS}",
                (body.title, body.emoji, body.cost, reward_id),
            )

    @api.delete("/v1/rewards/{reward_id}", status_code=204, tags=["Points"], summary="Remove a reward")
    def remove_reward(reward_id: UUID, user: User) -> None:
        """Takes a reward out of the shop. Requests already made are kept."""
        with transaction(settings, user) as conn:
            one(conn, "UPDATE hq.rewards SET is_active=false WHERE id=%s RETURNING id", (reward_id,))

    @api.get("/v1/kid/rewards", tags=["Kid mode"], summary="The shop")
    def kid_rewards(kid: Kid) -> list[dict]:
        """With a kid token: the family's active rewards, cheapest first."""
        with transaction(settings, kid=kid) as conn:
            return conn.execute(
                f"SELECT {REWARD_COLUMNS} FROM hq.rewards WHERE is_active ORDER BY cost,title"
            ).fetchall()

    @api.post("/v1/kid/reward-requests", status_code=201, tags=["Kid mode"], summary="Ask for a reward")
    def ask_for_reward(body: RewardRequest, kid: Kid) -> dict:
        """With a kid token: spends points on a reward. The points are held until a caregiver approves, or
        declines and they come back. A mystery box picks a prize here; a jackpot pays straight away. 422 when
        the child does not have enough points.
        """
        with transaction(settings, kid=kid) as conn:
            reward = one(
                conn, f"SELECT {REWARD_COLUMNS} FROM hq.rewards WHERE id=%s AND is_active", (body.reward_id,)
            )
            available = balance(conn, kid)
            if reward["cost"] > available:
                raise HTTPException(422, f"You need {reward['cost'] - available} more points.")
            item = {
                "title": reward["title"],
                "emoji": reward["emoji"],
                "minutes": reward["minutes"],
                "jackpot": 0,
            }
            mystery = reward["kind"] == "mystery"
            if mystery:
                item = {"minutes": None, "jackpot": 0} | random.choices(
                    MYSTERY_PRIZES, weights=[p["weight"] for p in MYSTERY_PRIZES]
                )[0]
            conn.execute("SELECT hq.kid_ledger(%s,'spent',%s)", (-reward["cost"], reward["title"]))
            if item["jackpot"]:
                conn.execute("SELECT hq.kid_ledger(%s,'bonus','Mystery box jackpot')", (item["jackpot"],))
            request = one(
                conn,
                "INSERT INTO hq.reward_requests(child_id,reward_id,title,emoji,cost,minutes,mystery,status,day)"
                f" VALUES(%s,%s,%s,%s,%s,%s,%s,%s,hq.child_today(%s)) RETURNING {REQUEST_COLUMNS}",
                (
                    kid,
                    reward["id"],
                    item["title"],
                    item["emoji"],
                    reward["cost"],
                    item["minutes"],
                    mystery,
                    "approved" if item["jackpot"] else "pending",
                    kid,
                ),
            )
            return {"request": request, "points": balance(conn, kid)}

    @api.get("/v1/kid/reward-requests", tags=["Kid mode"], summary="My reward requests")
    def kid_requests(kid: Kid, limit: Limit = 50) -> list[dict]:
        """With a kid token: this child's reward requests, newest first, with `status`."""
        with transaction(settings, kid=kid) as conn:
            return conn.execute(
                f"SELECT {REQUEST_COLUMNS} FROM hq.reward_requests ORDER BY created_at DESC,id LIMIT %s",
                (limit,),
            ).fetchall()

    @api.get("/v1/families/{family_id}/reward-requests", tags=["Points"], summary="Reward requests")
    def pending_requests(family_id: UUID, user: User) -> list[dict]:
        """Reward requests in the family waiting for a caregiver, oldest first."""
        with transaction(settings, user) as conn:
            one(conn, "SELECT id FROM hq.families WHERE id=%s", (family_id,))
            return conn.execute(
                "SELECT r.id,r.child_id,k.name AS child_name,r.title,r.emoji,r.cost,r.minutes,r.mystery,r.status,"
                "r.day,r.created_at FROM hq.reward_requests r JOIN hq.children k ON k.id=r.child_id"
                " WHERE k.family_id=%s AND r.status='pending' ORDER BY r.created_at,r.id",
                (family_id,),
            ).fetchall()

    def decide(request_id: UUID, user: UUID, status: str, pin: str | None) -> dict:
        with transaction(settings, user) as conn:
            row = one(
                conn,
                "SELECT r.child_id,r.title,r.cost,r.minutes,r.status,f.pin_hash FROM hq.reward_requests r"
                " JOIN hq.children k ON k.id=r.child_id JOIN hq.families f ON f.id=k.family_id"
                " WHERE r.id=%s FOR UPDATE OF r",
                (request_id,),
            )
            settings.pin_attempts.check(str(user))
            if status == "approved" and not check_pin(pin or "", row["pin_hash"]):
                raise HTTPException(403, "Wrong PIN")
            if row["status"] != "pending":
                raise HTTPException(409, f"This request was already {row['status']}")
            if status == "approved" and row["minutes"]:
                add_to_day(conn, "screen_bonus", row["child_id"], row["minutes"])
            if status == "declined":
                conn.execute(
                    "INSERT INTO hq.points_ledger(child_id,amount,reason,note,created_by)"
                    " VALUES(%s,%s,'refund',%s,%s)",
                    (row["child_id"], row["cost"], f"Refund: {row['title']}"[:200], user),
                )
            conn.execute(
                "UPDATE hq.reward_requests SET status=%s,decided_by=%s,decided_at=now() WHERE id=%s",
                (status, user, request_id),
            )
            return {"status": status, "child_id": row["child_id"], "points": balance(conn, row["child_id"])}

    @api.post("/v1/reward-requests/{request_id}/approve", tags=["Points"], summary="Approve a reward")
    def approve_request(request_id: UUID, body: PinConfirm, user: User) -> dict:
        """Approves a request after checking the family PIN (403 if wrong). Screen-time rewards add minutes to
        today's `screen_bonus`. 409 when it was already decided.
        """
        return decide(request_id, user, "approved", body.pin)

    @api.post("/v1/reward-requests/{request_id}/decline", tags=["Points"], summary="Not this time")
    def decline_request(request_id: UUID, user: User) -> dict:
        """Declines a request and gives the points back. 409 when it was already decided."""
        return decide(request_id, user, "declined", None)

    return api
