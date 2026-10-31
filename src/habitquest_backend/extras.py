"""What kid mode keeps per child (worlds found, the chest, the wardrobe, time in the app) and family settings."""

import random
from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from psycopg.types.json import Jsonb
from pydantic import Field

from habitquest_backend.accounts import StrictModel
from habitquest_backend.auth import KID, check_pin, signed_in
from habitquest_backend.config import Settings
from habitquest_backend.daily import balance, today_view
from habitquest_backend.db import one, transaction

ACCESSORIES = {
    "party-hat": "Party hat",
    "crown": "Golden crown",
    "shades": "Cool shades",
    "bow": "Big bow",
    "wizard": "Wizard hat",
    "flower": "Flower",
    "headphones": "Headphones",
    "halo": "Star halo",
}
ACCESSORY_CHANCE = 0.45
EXTRAS_COLUMNS = (
    "discoveries,accessories,wearing,goal,chests,level_seen,app_seconds,app_extra_minutes,screen_bonus"
)
APP_LIMIT_OPTIONS = {0, 15, 20, 30, 45, 60}


class ExtrasUpdate(StrictModel):
    discover: str | None = Field(
        default=None, max_length=80, description='A "<world>:<item>" key the child found'
    )
    wearing: str | None = Field(default=None, description='An accessory the child owns, or "" to take it off')
    goal: UUID | None = None
    level_seen: int | None = Field(default=None, ge=1, le=5)


class AppTime(StrictModel):
    seconds: int = Field(gt=0, le=300, description="Seconds used since the last report")


class AppExtension(StrictModel):
    minutes: int = Field(gt=0, le=120)
    pin: str = Field(pattern=r"^\d{4}$")


class SettingsUpdate(StrictModel):
    app_limit: int | None = Field(default=None, description="Daily minutes in kid mode; 0 means no limit")
    buddy_talk: bool | None = None


def extras_view(conn, child_id: UUID) -> dict:
    extras = one(conn, f"SELECT {EXTRAS_COLUMNS} FROM hq.child_extras WHERE child_id=%s", (child_id,))
    settings_row = one(
        conn,
        "SELECT s.app_limit,s.buddy_talk FROM hq.family_settings s JOIN hq.children k ON k.family_id=s.family_id"
        " WHERE k.id=%s",
        (child_id,),
    )
    return {**extras, **settings_row}


def add_to_day(conn, column: str, child_id: UUID, amount: int) -> None:
    """Adds `amount` to today's entry in a per-day map, keeping two weeks."""
    row = conn.execute(
        f"SELECT {column} AS days,hq.child_today(%s) AS today FROM hq.child_extras WHERE child_id=%s",
        (child_id, child_id),
    ).fetchone()
    today, oldest = row["today"].isoformat(), row["today"].toordinal() - 14
    days = {day: value for day, value in row["days"].items() if date.fromisoformat(day).toordinal() >= oldest}
    days[today] = days.get(today, 0) + amount
    conn.execute(f"UPDATE hq.child_extras SET {column}=%s WHERE child_id=%s", (Jsonb(days), child_id))


def router(settings: Settings) -> APIRouter:
    api = APIRouter()
    User = Annotated[UUID, Depends(signed_in(settings))]
    Kid = Annotated[UUID, Depends(signed_in(settings, KID))]

    @api.get("/v1/kid/extras", tags=["Kid mode"], summary="Worlds, wardrobe and time")
    def kid_extras(kid: Kid) -> dict:
        """With a kid token: worlds found, accessories, days the chest was opened, time used per day, and the
        family's `app_limit` and `buddy_talk` settings.
        """
        with transaction(settings, kid=kid) as conn:
            return extras_view(conn, kid)

    @api.patch("/v1/kid/extras", tags=["Kid mode"], summary="Save a discovery or outfit")
    def update_extras(body: ExtrasUpdate, kid: Kid) -> dict:
        """With a kid token: records a `discover` key, the accessory the child is `wearing` (it must be owned;
        422 otherwise), their savings `goal`, and the highest level seen celebrated.
        """
        with transaction(settings, kid=kid) as conn:
            owned = one(conn, "SELECT accessories FROM hq.child_extras WHERE child_id=%s", (kid,))[
                "accessories"
            ]
            if body.wearing and body.wearing not in owned:
                raise HTTPException(422, "That accessory hasn't been found yet")
            if body.discover:
                conn.execute(
                    "UPDATE hq.child_extras SET discoveries=array_append(discoveries,%s)"
                    " WHERE child_id=%s AND NOT %s=ANY(discoveries)",
                    (body.discover, kid, body.discover),
                )
            if body.wearing is not None:
                conn.execute(
                    "UPDATE hq.child_extras SET wearing=%s WHERE child_id=%s", (body.wearing or None, kid)
                )
            if body.goal:
                one(conn, "SELECT id FROM hq.rewards WHERE id=%s AND is_active", (body.goal,))
                conn.execute("UPDATE hq.child_extras SET goal=%s WHERE child_id=%s", (body.goal, kid))
            if body.level_seen:
                conn.execute(
                    "UPDATE hq.child_extras SET level_seen=greatest(level_seen,%s) WHERE child_id=%s",
                    (body.level_seen, kid),
                )
            return extras_view(conn, kid)

    @api.post("/v1/kid/chest", tags=["Kid mode"], summary="Open today's chest")
    def open_chest(kid: Kid) -> dict:
        """With a kid token: once a day, after every habit today is approved, opens the chest: a new buddy
        accessory, or 5-25 bonus points. The server picks the prize. 409 if it is not ready or already opened.
        """
        with transaction(settings, kid=kid) as conn:
            view = today_view(conn, kid)
            if not view["habits"] or any(h["status"] != "approved" for h in view["habits"]):
                raise HTTPException(409, "Finish today's quests first")
            extras = one(
                conn, "SELECT accessories,chests FROM hq.child_extras WHERE child_id=%s FOR UPDATE", (kid,)
            )
            if view["day"] in extras["chests"]:
                raise HTTPException(409, "Today's chest is already open")
            conn.execute(
                "UPDATE hq.child_extras SET chests=array_append(chests,%s) WHERE child_id=%s",
                (view["day"], kid),
            )
            unowned = [a for a in ACCESSORIES if a not in extras["accessories"]]
            if unowned and random.random() < ACCESSORY_CHANCE:
                accessory = random.choice(unowned)
                prize: dict = {"kind": "accessory", "id": accessory, "name": ACCESSORIES[accessory]}
                conn.execute(
                    "UPDATE hq.child_extras SET accessories=array_append(accessories,%s),wearing=%s WHERE child_id=%s",
                    (accessory, accessory, kid),
                )
            else:
                prize = {"kind": "points", "points": 5 * random.randint(1, 5)}
                conn.execute("SELECT hq.kid_ledger(%s,'bonus','Treasure chest')", (prize["points"],))
            return {"prize": prize, "points": balance(conn, kid), "extras": extras_view(conn, kid)}

    @api.post("/v1/kid/app-time", tags=["Kid mode"], summary="Report time in the app")
    def report_time(body: AppTime, kid: Kid) -> dict:
        """With a kid token: adds seconds used today, at most 300 per report. Returns the updated extras."""
        with transaction(settings, kid=kid) as conn:
            add_to_day(conn, "app_seconds", kid, body.seconds)
            return extras_view(conn, kid)

    @api.get("/v1/children/{child_id}/extras", tags=["Children"], summary="A child's extras")
    def child_extras(child_id: UUID, user: User) -> dict:
        """The same extras the child sees in kid mode. 404 unless the caller cares for the child."""
        with transaction(settings, user) as conn:
            return extras_view(conn, child_id)

    @api.post("/v1/children/{child_id}/app-time/extend", tags=["Children"], summary="Allow more time today")
    def extend_time(child_id: UUID, body: AppExtension, user: User) -> dict:
        """Adds extra kid-mode minutes for today after checking the family PIN (403 if wrong)."""
        with transaction(settings, user) as conn:
            family = one(
                conn,
                "SELECT f.pin_hash FROM hq.children k JOIN hq.families f ON f.id=k.family_id WHERE k.id=%s",
                (child_id,),
            )
            if not check_pin(body.pin, family["pin_hash"]):
                raise HTTPException(403, "Wrong PIN")
            add_to_day(conn, "app_extra_minutes", child_id, body.minutes)
            return extras_view(conn, child_id)

    @api.get("/v1/families/{family_id}/settings", tags=["Families"], summary="Family settings")
    def read_settings(family_id: UUID, user: User) -> dict:
        """The daily kid-mode time limit `app_limit` (0 means no limit), and whether the buddy may use AI lines."""
        with transaction(settings, user) as conn:
            return one(
                conn, "SELECT app_limit,buddy_talk FROM hq.family_settings WHERE family_id=%s", (family_id,)
            )

    @api.patch("/v1/families/{family_id}/settings", tags=["Families"], summary="Change family settings")
    def change_settings(family_id: UUID, body: SettingsUpdate, user: User) -> dict:
        """Changes `app_limit` (0, 15, 20, 30, 45 or 60 minutes; 422 otherwise) or `buddy_talk`."""
        if body.app_limit is not None and body.app_limit not in APP_LIMIT_OPTIONS:
            raise HTTPException(422, "Pick 0, 15, 20, 30, 45 or 60 minutes")
        with transaction(settings, user) as conn:
            return one(
                conn,
                "UPDATE hq.family_settings SET app_limit=coalesce(%s,app_limit),buddy_talk=coalesce(%s,buddy_talk)"
                " WHERE family_id=%s RETURNING app_limit,buddy_talk",
                (body.app_limit, body.buddy_talk, family_id),
            )

    return api
