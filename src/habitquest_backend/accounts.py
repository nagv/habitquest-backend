"""Google sign-in, families and their caregivers, the family PIN, and kid sessions."""

import secrets
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from habitquest_backend.auth import KID, check_pin, hash_pin, issue_token, signed_in, verify_google
from habitquest_backend.config import Settings
from habitquest_backend.db import one, transaction
from habitquest_backend.limits import client_address

SESSION_MINUTES = 8 * 60
# Unambiguous characters (no 0/O, 1/I/L): 8 of 31 gives about 4 * 10^11 codes.
INVITE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
Role = Literal["father", "mother", "guardian", "other"]
Pin = Annotated[str, Field(pattern=r"^\d{4}$", description="Four digits")]


CHILD_EXPORT = {
    "habits": "SELECT habit_id,cue_time::text,days,status,started_on,ended_on FROM hq.child_habits WHERE child_id=%s",
    "completions": "SELECT ch.habit_id,c.day,c.status,c.submitted_at FROM hq.completions c"
    " JOIN hq.child_habits ch ON ch.id=c.child_habit_id WHERE ch.child_id=%s ORDER BY c.day",
    "points": "SELECT amount,reason,note,created_at FROM hq.points_ledger WHERE child_id=%s ORDER BY id",
    "pain_points": "SELECT text,mapped_by,created_at FROM hq.pain_points WHERE child_id=%s ORDER BY created_at",
    "checkins": "SELECT day,mood,hardest,favorite,stickers,note,flagged FROM hq.checkins WHERE child_id=%s ORDER BY day",
    "coach_messages": "SELECT role,content,created_at FROM hq.coach_messages WHERE child_id=%s ORDER BY id",
    "ai_suggestions": "SELECT source,kind,habit_id,reason,status,created_at FROM hq.ai_suggestions WHERE child_id=%s",
    "reward_requests": "SELECT title,cost,status,day FROM hq.reward_requests WHERE child_id=%s ORDER BY created_at",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def valid_timezone(value: str | None) -> str | None:
    try:
        if value is not None:
            ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Use an IANA time zone such as Europe/London") from exc
    return value


class GoogleLogin(StrictModel):
    credential: str = Field(min_length=20, max_length=4096, description="The ID token from Google's button")


class CaregiverInput(StrictModel):
    display_name: str = Field(min_length=1, max_length=40, description="How the family sees this caregiver")
    role: Role


class FamilyCreate(CaregiverInput):
    name: str = Field(min_length=1, max_length=60)
    timezone: str = "UTC"
    pin: Pin

    _timezone = field_validator("timezone")(valid_timezone)


class FamilyUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    timezone: str | None = None

    _timezone = field_validator("timezone")(valid_timezone)


class JoinInput(CaregiverInput):
    invite_code: str = Field(min_length=4, max_length=20)


class CaregiverUpdate(StrictModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=40)
    role: Role | None = None


class PinInput(StrictModel):
    pin: Pin


def invite_code() -> str:
    return "".join(secrets.choice(INVITE_ALPHABET) for _ in range(8))


def family_view(conn, family_id: UUID) -> dict:
    family = one(
        conn,
        "SELECT id,name,timezone,invite_code,pin_hash IS NOT NULL AS has_pin,created_at"
        " FROM hq.families WHERE id=%s",
        (family_id,),
    )
    family["caregivers"] = conn.execute(
        "SELECT user_id,display_name,role FROM hq.caregivers WHERE family_id=%s ORDER BY created_at,user_id",
        (family_id,),
    ).fetchall()
    return family


def token_response(token: str, minutes: int) -> dict:
    return {"access_token": token, "token_type": "bearer", "expires_in": minutes * 60}


def router(settings: Settings) -> APIRouter:
    api = APIRouter()
    User = Annotated[UUID, Depends(signed_in(settings))]
    Kid = Annotated[UUID, Depends(signed_in(settings, KID))]

    @api.post("/v1/auth/google", tags=["Auth"], summary="Sign in with Google")
    def google(body: GoogleLogin, request: Request) -> dict:
        """Exchanges the ID token from Google's sign-in button (`credential`) for our bearer token, valid for
        8 hours. The Google account must have a verified email; the user record is created on first sign-in
        and its email and name refreshed on later ones. 401 for an invalid or unverified token, 503 when
        Google sign-in is not configured (`GOOGLE_CLIENT_ID`) or Google's keys cannot be fetched, 429 after
        too many attempts in a minute.
        """
        settings.sign_in_attempts.check(client_address(request))
        claims = verify_google(body.credential, settings)
        name = (claims.get("name") or claims["email"].split("@")[0])[:80]
        with transaction(settings) as conn:
            user = conn.execute(
                "SELECT hq.google_user(%s,%s,%s) AS id", (claims["sub"], claims["email"], name)
            ).fetchone()["id"]
        return token_response(issue_token(settings, user, minutes=SESSION_MINUTES), SESSION_MINUTES)

    @api.get("/v1/me", tags=["Families"], summary="Who am I")
    def me(user: User) -> dict:
        """The signed-in user (`id`, `name`, `email`) and the families they care for, with their
        `display_name` and `role` in each. An empty `families` list means the next step is to create a
        family or join one with an invite code.
        """
        with transaction(settings, user) as conn:
            profile = one(conn, "SELECT id,name,email FROM hq.users WHERE id=%s", (user,))
            profile["families"] = conn.execute(
                "SELECT f.id,f.name,c.display_name,c.role FROM hq.families f"
                " JOIN hq.caregivers c ON c.family_id=f.id AND c.user_id=%s ORDER BY c.created_at",
                (user,),
            ).fetchall()
            return profile

    @api.post("/v1/families", status_code=201, tags=["Families"], summary="Create a family")
    def create_family(body: FamilyCreate, user: User) -> dict:
        """Creates a family with the caller as its first caregiver (`display_name`, `role`: father, mother,
        guardian or other). `timezone` (IANA, default UTC) decides what "today" is for the children's habits.
        `pin` (four digits) unlocks grown-up screens on a shared device. The response includes the
        `invite_code` other caregivers use to join.
        """
        with transaction(settings, user) as conn:
            family = conn.execute(
                "SELECT hq.create_family(%s,%s,%s,%s,%s,%s) AS id",
                (body.name, body.timezone, hash_pin(body.pin), invite_code(), body.display_name, body.role),
            ).fetchone()["id"]
            return family_view(conn, family)

    @api.post("/v1/families/join", tags=["Families"], summary="Join a family")
    def join_family(body: JoinInput, user: User) -> dict:
        """Joins the family whose `invite_code` this is, as a caregiver with `display_name` and `role`. The
        code is not case-sensitive and may include spaces or dashes. 404 for an unknown code, 409 when the
        caller already cares for that family.
        """
        code = "".join(body.invite_code.upper().split()).replace("-", "")
        with transaction(settings, user) as conn:
            try:
                family = conn.execute(
                    "SELECT hq.join_family(%s,%s,%s) AS id", (code, body.display_name, body.role)
                ).fetchone()["id"]
            except psycopg.errors.NoDataFound as exc:
                raise HTTPException(404, "Unknown invite code") from exc
            except psycopg.errors.UniqueViolation as exc:
                raise HTTPException(409, "You already care for this family") from exc
            return family_view(conn, family)

    @api.get("/v1/families/{family_id}", tags=["Families"], summary="Read a family")
    def read_family(family_id: UUID, user: User) -> dict:
        """The family's name, time zone, invite code, whether a PIN is set (`has_pin`; the PIN itself is
        never returned) and its caregivers. 404 unless the caller cares for this family.
        """
        with transaction(settings, user) as conn:
            return family_view(conn, family_id)

    @api.patch("/v1/families/{family_id}", tags=["Families"], summary="Edit a family")
    def update_family(family_id: UUID, body: FamilyUpdate, user: User) -> dict:
        """Changes the family's `name` and/or `timezone`; omitted fields stay as they are. Any caregiver of
        the family may edit it.
        """
        changes = body.model_dump(exclude_none=True)
        with transaction(settings, user) as conn:
            one(conn, "SELECT id FROM hq.families WHERE id=%s", (family_id,))
            for column, value in changes.items():
                conn.execute(f"UPDATE hq.families SET {column}=%s WHERE id=%s", (value, family_id))
            return family_view(conn, family_id)

    @api.patch(
        "/v1/families/{family_id}/caregivers/me", tags=["Families"], summary="Edit my caregiver details"
    )
    def update_me(family_id: UUID, body: CaregiverUpdate, user: User) -> dict:
        """Changes the caller's own `display_name` and/or `role` in this family; omitted fields stay as they
        are. Returns the family.
        """
        changes = body.model_dump(exclude_none=True)
        with transaction(settings, user) as conn:
            one(
                conn, "SELECT user_id FROM hq.caregivers WHERE family_id=%s AND user_id=%s", (family_id, user)
            )
            for column, value in changes.items():
                conn.execute(
                    f"UPDATE hq.caregivers SET {column}=%s WHERE family_id=%s AND user_id=%s",
                    (value, family_id, user),
                )
            return family_view(conn, family_id)

    @api.post("/v1/families/{family_id}/invite-code", tags=["Families"], summary="Replace the invite code")
    def new_invite_code(family_id: UUID, user: User) -> dict:
        """Replaces the family's invite code, for example after sharing it with the wrong person. The old code
        stops working at once; caregivers who already joined stay. Returns the family with the new code.
        """
        with transaction(settings, user) as conn:
            one(conn, "SELECT id FROM hq.families WHERE id=%s", (family_id,))
            conn.execute("UPDATE hq.families SET invite_code=%s WHERE id=%s", (invite_code(), family_id))
            return family_view(conn, family_id)

    @api.put("/v1/families/{family_id}/pin", status_code=204, tags=["Families"], summary="Change the PIN")
    def set_pin(family_id: UUID, body: PinInput, user: User) -> Response:
        """Sets a new four-digit family PIN. Any caregiver of the family may change it; it is stored only as a
        salted hash.
        """
        with transaction(settings, user) as conn:
            one(conn, "SELECT id FROM hq.families WHERE id=%s", (family_id,))
            conn.execute("UPDATE hq.families SET pin_hash=%s WHERE id=%s", (hash_pin(body.pin), family_id))
        return Response(status_code=204)

    @api.post("/v1/families/{family_id}/pin/verify", tags=["Families"], summary="Check the PIN")
    def verify_pin(family_id: UUID, body: PinInput, user: User) -> dict:
        """Returns `{"valid": true}` when `pin` matches the family PIN, otherwise `{"valid": false}`. The
        frontend asks for the PIN before leaving kid mode or opening a grown-up screen on a shared device.
        """
        settings.pin_attempts.check(str(user))
        with transaction(settings, user) as conn:
            stored = one(conn, "SELECT pin_hash FROM hq.families WHERE id=%s", (family_id,))["pin_hash"]
        return {"valid": check_pin(body.pin, stored)}

    @api.get("/v1/families/{family_id}/export", tags=["Families"], summary="Export family data")
    def export_family(family_id: UUID, user: User) -> dict:
        """Everything stored for the family as JSON: the family, caregivers, children and each child's plan,
        completions, points, check-ins, coach conversation and AI suggestions. The PIN hash is left out.
        """
        with transaction(settings, user) as conn:
            family = family_view(conn, family_id)
            children = conn.execute(
                "SELECT id,name,age,buddy,world,routine_times,created_at FROM hq.children WHERE family_id=%s"
                " ORDER BY created_at,id",
                (family_id,),
            ).fetchall()
            for child in children:
                for name, sql in CHILD_EXPORT.items():
                    child[name] = conn.execute(sql, (child["id"],)).fetchall()
            return {"family": family, "children": children}

    @api.post(
        "/v1/families/{family_id}/delete", status_code=204, tags=["Families"], summary="Delete a family"
    )
    def delete_family(family_id: UUID, body: PinInput, user: User) -> Response:
        """Permanently deletes the family and everything about its children, after checking the family PIN
        (403 if wrong). This cannot be undone. Caregivers keep their sign-in.
        """
        settings.pin_attempts.check(str(user))
        with transaction(settings, user) as conn:
            family = one(conn, "SELECT pin_hash FROM hq.families WHERE id=%s", (family_id,))
            if not check_pin(body.pin, family["pin_hash"]):
                raise HTTPException(403, "Wrong PIN")
            conn.execute("DELETE FROM hq.families WHERE id=%s", (family_id,))
        return Response(status_code=204)

    @api.post("/v1/children/{child_id}/delete", status_code=204, tags=["Children"], summary="Delete a child")
    def delete_child(child_id: UUID, body: PinInput, user: User) -> Response:
        """Permanently deletes a child and all their data, after checking the family PIN (403 if wrong)."""
        settings.pin_attempts.check(str(user))
        with transaction(settings, user) as conn:
            family = one(
                conn,
                "SELECT f.pin_hash FROM hq.children k JOIN hq.families f ON f.id=k.family_id WHERE k.id=%s",
                (child_id,),
            )
            if not check_pin(body.pin, family["pin_hash"]):
                raise HTTPException(403, "Wrong PIN")
            conn.execute("DELETE FROM hq.children WHERE id=%s", (child_id,))
        return Response(status_code=204)

    @api.delete(
        "/v1/families/{family_id}/caregivers/me", status_code=204, tags=["Families"], summary="Leave a family"
    )
    def leave_family(family_id: UUID, user: User) -> Response:
        """Removes the signed-in caregiver from the family. The last caregiver cannot leave (409): delete the
        family instead.
        """
        with transaction(settings, user) as conn:
            count = one(conn, "SELECT count(*) AS n FROM hq.caregivers WHERE family_id=%s", (family_id,))["n"]
            if count == 0:
                raise HTTPException(404, "Record not found")
            if count == 1:
                raise HTTPException(409, "You are the last caregiver; delete the family instead")
            conn.execute("DELETE FROM hq.caregivers WHERE family_id=%s AND user_id=%s", (family_id, user))
        return Response(status_code=204)

    @api.post("/v1/children/{child_id}/kid-session", tags=["Kid mode"], summary="Start kid mode")
    def kid_session(child_id: UUID, user: User) -> dict:
        """A caregiver starts kid mode for one of their children on this device. Returns a kid token, valid
        for 8 hours, that can only use the kid endpoints for that child; caregiver endpoints refuse it.
        404 unless the caller cares for the child.
        """
        with transaction(settings, user) as conn:
            child = one(conn, "SELECT id,name FROM hq.children WHERE id=%s", (child_id,))
        response = token_response(
            issue_token(settings, child_id, minutes=SESSION_MINUTES, role=KID), SESSION_MINUTES
        )
        return {**response, "child": child}

    @api.get("/v1/kid/me", tags=["Kid mode"], summary="Who is playing")
    def kid_me(kid: Kid) -> dict:
        """With a kid token: the child it belongs to (`id`, `name`, `age`, `buddy`, `world`), for the kid
        screens' greeting and theme.
        """
        with transaction(settings, kid=kid) as conn:
            return one(conn, "SELECT id,name,age,buddy,world FROM hq.children WHERE id=%s", (kid,))

    return api
