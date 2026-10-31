"""Kid check-ins, which go through the safety screen, and the surprises a parent plants for kid mode."""

from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field, field_validator, model_validator

from habitquest_backend.accounts import StrictModel
from habitquest_backend.auth import KID, signed_in
from habitquest_backend.config import Settings
from habitquest_backend.daily import balance
from habitquest_backend.db import one, transaction
from habitquest_backend.safety import KID_BLOCKED_MESSAGE, KID_SAFETY_MESSAGE, check_text, screen_text

STICKERS = {"best", "proud", "help", "together", "too-hard", "new-quest"}
CHECKIN_COLUMNS = "id,day,mood,hardest,favorite,stickers,note,flagged,read_at,created_at"
SURPRISE_COLUMNS = "id,child_id,kind,message,points,day,created_at"
Limit = Annotated[int, Query(ge=1, le=200, description="How many entries, newest first")]


class CheckinInput(StrictModel):
    mood: Literal["great", "good", "okay", "hard"]
    hardest: str | None = None
    favorite: str | None = None
    stickers: list[str] = Field(default_factory=list, max_length=6)
    note: str | None = Field(default=None, max_length=200)

    @field_validator("stickers")
    @classmethod
    def known_stickers(cls, stickers: list[str]) -> list[str]:
        if not set(stickers) <= STICKERS:
            raise ValueError("Unknown sticker")
        return stickers


class SurpriseInput(StrictModel):
    kind: Literal["note", "double"]
    child_id: UUID | None = Field(default=None, description="Leave out for every child")
    message: str = Field(default="", max_length=140)
    points: int = Field(default=0, ge=0, le=100)
    day: date

    @model_validator(mode="after")
    def double_days_have_no_points(self) -> "SurpriseInput":
        if self.kind == "double" and self.points:
            raise ValueError("A double-points day has no bonus points")
        return self


def router(settings: Settings) -> APIRouter:
    api = APIRouter()
    User = Annotated[UUID, Depends(signed_in(settings))]
    Kid = Annotated[UUID, Depends(signed_in(settings, KID))]

    @api.post("/v1/kid/checkins", tags=["Kid mode"], summary="Send a check-in")
    def send_checkin(body: CheckinInput, kid: Kid) -> dict:
        """With a kid token: today's mood, trickiest and favourite habit, stickers and an optional short note. A
        newer check-in replaces today's. A note is screened first: an unsafe one is refused (422) with a friendly
        message to show the child. A note suggesting the child may be at risk is kept and `flagged` for the
        parents, and `message` asks the child to talk to a grown-up they trust.
        """
        screen = check_text(settings, body.note, "kid-writes") if body.note else None
        if screen and not screen.ok and not screen.risk:
            raise HTTPException(422, KID_BLOCKED_MESSAGE[screen.category or "unkind"])
        flagged = screen.category if screen and screen.risk else None
        with transaction(settings, kid=kid) as conn:
            day = conn.execute("SELECT hq.child_today(%s) AS day", (kid,)).fetchone()["day"]
            saved = one(
                conn,
                "INSERT INTO hq.checkins(child_id,day,mood,hardest,favorite,stickers,note,flagged)"
                " VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (child_id,day) DO UPDATE SET mood=EXCLUDED.mood,"
                "hardest=EXCLUDED.hardest,favorite=EXCLUDED.favorite,stickers=EXCLUDED.stickers,note=EXCLUDED.note,"
                f"flagged=EXCLUDED.flagged,read_at=NULL RETURNING {CHECKIN_COLUMNS}",
                (kid, day, body.mood, body.hardest, body.favorite, body.stickers, body.note, flagged),
            )
        return {"checkin": saved, "message": KID_SAFETY_MESSAGE if flagged else None}

    @api.get("/v1/kid/checkins/today", tags=["Kid mode"], summary="Today's check-in")
    def today_checkin(kid: Kid) -> dict | None:
        """With a kid token: the check-in already sent today, or `null`."""
        with transaction(settings, kid=kid) as conn:
            return conn.execute(
                f"SELECT {CHECKIN_COLUMNS} FROM hq.checkins WHERE child_id=%s AND day=hq.child_today(%s)",
                (kid, kid),
            ).fetchone()

    @api.get("/v1/families/{family_id}/checkins", tags=["Daily"], summary="Check-ins from the kids")
    def family_checkins(family_id: UUID, user: User, limit: Limit = 50) -> list[dict]:
        """The children's check-ins, newest first, with `read_at` empty until a caregiver marks them read and
        `flagged` naming any safety concern in the note.
        """
        with transaction(settings, user) as conn:
            one(conn, "SELECT id FROM hq.families WHERE id=%s", (family_id,))
            return conn.execute(
                "SELECT c.id,c.child_id,k.name AS child_name,c.day,c.mood,c.hardest,c.favorite,c.stickers,c.note,"
                "c.flagged,c.read_at,c.created_at FROM hq.checkins c JOIN hq.children k ON k.id=c.child_id"
                " WHERE k.family_id=%s ORDER BY c.created_at DESC,c.id LIMIT %s",
                (family_id, limit),
            ).fetchall()

    @api.post("/v1/families/{family_id}/checkins/read", tags=["Daily"], summary="Mark check-ins read")
    def read_checkins(family_id: UUID, user: User) -> dict:
        """Marks every unread check-in in the family as read and returns how many changed."""
        with transaction(settings, user) as conn:
            one(conn, "SELECT id FROM hq.families WHERE id=%s", (family_id,))
            marked = conn.execute(
                "UPDATE hq.checkins c SET read_at=now() FROM hq.children k"
                " WHERE k.id=c.child_id AND k.family_id=%s AND c.read_at IS NULL",
                (family_id,),
            ).rowcount
            return {"marked": marked}

    @api.post(
        "/v1/families/{family_id}/surprises", status_code=201, tags=["Daily"], summary="Plant a surprise"
    )
    def plant(family_id: UUID, body: SurpriseInput, user: User) -> dict:
        """Plants a surprise: a `note` with up to 100 bonus points, or a `double` points `day`. Leave `child_id`
        out for every child. A message unsuitable for a child is refused (422).
        """
        if body.message and not screen_text(body.message).ok:
            raise HTTPException(422, "Please reword that message: it may not be suitable for a child.")
        with transaction(settings, user) as conn:
            one(conn, "SELECT id FROM hq.families WHERE id=%s", (family_id,))
            if body.child_id:
                one(
                    conn,
                    "SELECT id FROM hq.children WHERE id=%s AND family_id=%s",
                    (body.child_id, family_id),
                )
            return one(
                conn,
                "INSERT INTO hq.surprises(family_id,child_id,kind,message,points,day,created_by)"
                f" VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING {SURPRISE_COLUMNS}",
                (family_id, body.child_id, body.kind, body.message, body.points, body.day, user),
            )

    @api.get("/v1/families/{family_id}/surprises", tags=["Daily"], summary="Surprises not yet found")
    def planted(family_id: UUID, user: User) -> list[dict]:
        """Surprises at least one intended child has not opened yet, oldest first."""
        with transaction(settings, user) as conn:
            one(conn, "SELECT id FROM hq.families WHERE id=%s", (family_id,))
            return conn.execute(
                f"SELECT {SURPRISE_COLUMNS} FROM hq.surprises s WHERE s.family_id=%s AND EXISTS("
                " SELECT 1 FROM hq.children k WHERE k.family_id=s.family_id AND (s.child_id IS NULL OR k.id=s.child_id)"
                " AND NOT EXISTS(SELECT 1 FROM hq.surprise_reveals r WHERE r.surprise_id=s.id AND r.child_id=k.id))"
                " ORDER BY s.created_at,s.id",
                (family_id,),
            ).fetchall()

    @api.get("/v1/kid/surprises", tags=["Kid mode"], summary="Surprises to find")
    def kid_surprises(kid: Kid) -> list[dict]:
        """With a kid token: surprises waiting for this child. Notes show from their day on, and double days
        only on their day.
        """
        with transaction(settings, kid=kid) as conn:
            return conn.execute(
                f"SELECT {SURPRISE_COLUMNS} FROM hq.surprises s WHERE NOT EXISTS("
                " SELECT 1 FROM hq.surprise_reveals r WHERE r.surprise_id=s.id AND r.child_id=%s)"
                " AND ((s.kind='note' AND s.day<=hq.child_today(%s)) OR s.day=hq.child_today(%s))"
                " ORDER BY s.created_at,s.id",
                (kid, kid, kid),
            ).fetchall()

    @api.post("/v1/kid/surprises/{surprise_id}/open", tags=["Kid mode"], summary="Open a surprise")
    def open_surprise(surprise_id: UUID, kid: Kid) -> dict:
        """With a kid token: opens a surprise once, paying any bonus points. Returns the surprise and the new
        balance. 409 when already opened, 404 when it is not for this child.
        """
        with transaction(settings, kid=kid) as conn:
            surprise = one(conn, f"SELECT {SURPRISE_COLUMNS} FROM hq.surprises WHERE id=%s", (surprise_id,))
            opened = conn.execute(
                "INSERT INTO hq.surprise_reveals(surprise_id,child_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                (surprise_id, kid),
            ).rowcount
            if not opened:
                raise HTTPException(409, "Already opened")
            if surprise["points"]:
                conn.execute(
                    "SELECT hq.kid_ledger(%s,'bonus','Surprise from a grown-up')", (surprise["points"],)
                )
            return {"surprise": surprise, "points": balance(conn, kid)}

    return api
