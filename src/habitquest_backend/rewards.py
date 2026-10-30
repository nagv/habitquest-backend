"""Spending points: caregivers record what points were used for, and everyone sees the points history."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field

from habitquest_backend.accounts import StrictModel
from habitquest_backend.auth import KID, signed_in
from habitquest_backend.config import Settings
from habitquest_backend.daily import balance
from habitquest_backend.db import one, transaction

Limit = Annotated[int, Query(ge=1, le=200, description="How many entries, newest first")]
ENTRY_COLUMNS = (
    "l.id,l.amount,l.reason,l.note,l.created_at,h.id AS habit_id,h.kid_title,h.icon,c.day AS completion_day"
)
ENTRY_JOINS = (
    " FROM hq.points_ledger l LEFT JOIN hq.completions c ON c.id=l.completion_id"
    " LEFT JOIN hq.child_habits ch ON ch.id=c.child_habit_id LEFT JOIN hq.habits h ON h.id=ch.habit_id"
)


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

    return api
