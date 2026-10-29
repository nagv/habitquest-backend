from typing import Annotated
from uuid import UUID, uuid4

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg.types.json import Jsonb

from habitquest_backend.auth import verify_token
from habitquest_backend.catalog.repository import load_release
from habitquest_backend.config import Settings
from habitquest_backend.db import one, transaction
from habitquest_backend.editorial.api import router as editorial_router
from habitquest_backend.schemas import ChildInput, Confirmation, IntakeInput, ProfileUpdate, ProposalPatch
from habitquest_backend.service import apply_patch, confirm, cue_loss_habits, enqueue, flag_plan

PROFILE_COLUMNS = "name,age_band,interests,accessibility,opted_in_topics,preferred_world,routine_schedule"


def profile_values(body: ChildInput) -> tuple:
    data = body.model_dump(mode="json")
    return (
        data["name"],
        data["age_band"],
        data["interests"],
        data["accessibility"],
        data["opted_in_topics"],
        data["preferred_world"],
        Jsonb(data["routine_schedule"]),
    )


def child_view(conn, child_id: UUID) -> dict:
    return one(
        conn,
        "SELECT c.*,h.timezone FROM hq.children c JOIN hq.households h ON h.id=c.household_id WHERE c.id=%s",
        (child_id,),
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    application = FastAPI(title="HabitQuest onboarding backend", version="0.2.0")
    bearer = HTTPBearer(auto_error=False)

    def parent(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> UUID:
        if credentials is None:
            raise HTTPException(401, "Parent authentication required")
        return verify_token(credentials.credentials, settings)

    Parent = Annotated[UUID, Depends(parent)]
    Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=100)]

    @application.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/health/ready")
    def ready():
        try:
            with transaction(settings) as conn:
                conn.execute("SELECT 1 FROM hq.children LIMIT 0")
        except psycopg.Error as exc:
            raise HTTPException(503, "Database or migrations unavailable") from exc
        return {"status": "ready"}

    @application.post("/v1/children", status_code=201)
    def add_child(body: ChildInput, user: Parent):
        with transaction(settings, user) as conn:
            # An upsert would need the row visible through the membership-based SELECT policy
            # before the membership exists, so insert first and set the timezone afterwards.
            conn.execute("INSERT INTO hq.households(id) VALUES(%s) ON CONFLICT DO NOTHING", (user,))
            conn.execute("INSERT INTO hq.memberships VALUES(%s,%s) ON CONFLICT DO NOTHING", (user, user))
            conn.execute("UPDATE hq.households SET timezone=%s WHERE id=%s", (body.timezone, user))
            child = one(
                conn,
                f"INSERT INTO hq.children(id,household_id,{PROFILE_COLUMNS}) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (uuid4(), user, *profile_values(body)),
            )
            return {**child, "timezone": body.timezone}

    @application.get("/v1/children/{child_id}")
    def get_child(child_id: UUID, user: Parent):
        with transaction(settings, user) as conn:
            return child_view(conn, child_id)

    @application.patch("/v1/children/{child_id}")
    def update_child(child_id: UUID, body: ProfileUpdate, user: Parent):
        with transaction(settings, user) as conn:
            old_child = conn.execute(
                "SELECT routine_schedule FROM hq.children WHERE id=%s", (child_id,)
            ).fetchone()
            updated = conn.execute(
                f"UPDATE hq.children SET ({PROFILE_COLUMNS},profile_version)="
                "(%s,%s,%s,%s,%s,%s,%s,profile_version+1) WHERE id=%s AND profile_version=%s RETURNING household_id",
                (*profile_values(body), child_id, body.expected_profile_version),
            ).fetchone()
            if updated is None:
                one(conn, "SELECT id FROM hq.children WHERE id=%s", (child_id,))
                raise HTTPException(409, "Profile changed; reload it before editing")
            conn.execute(
                "UPDATE hq.households SET timezone=%s WHERE id=%s", (body.timezone, updated["household_id"])
            )
            current_plan = conn.execute(
                "SELECT id,data FROM hq.plans WHERE child_id=%s ORDER BY version DESC LIMIT 1", (child_id,)
            ).fetchone()
            if current_plan:
                new_schedule = body.model_dump(mode="json")["routine_schedule"]
                for habit_id in cue_loss_habits(
                    old_child["routine_schedule"], new_schedule, current_plan["data"]["active"]
                ):
                    flag_plan(
                        conn,
                        current_plan["id"],
                        "cue_removed",
                        habit_id,
                        f"routine_schedule no longer contains the anchor for {habit_id}",
                    )
            return child_view(conn, child_id)

    @application.post("/v1/children/{child_id}/intakes", status_code=201)
    def add_intake(child_id: UUID, body: IntakeInput, user: Parent):
        with transaction(settings, user) as conn:
            one(conn, "SELECT id FROM hq.children WHERE id=%s", (child_id,))
            return one(
                conn,
                "INSERT INTO hq.intakes(id,child_id,parent_id,data) VALUES(%s,%s,%s,%s) RETURNING *",
                (uuid4(), child_id, user, Jsonb(body.model_dump())),
            )

    @application.post("/v1/intakes/{intake_id}/plan-runs", status_code=202)
    def start(intake_id: UUID, user: Parent, idempotency_key: Key):
        with transaction(settings, user) as conn:
            result = enqueue(conn, intake_id, user, idempotency_key)
            return {
                "run_id": result["id"],
                "status": result["status"],
                "status_url": f"/v1/plan-runs/{result['id']}",
            }

    @application.get("/v1/plan-runs/{run_id}")
    def status(run_id: UUID, user: Parent):
        with transaction(settings, user) as conn:
            return one(
                conn,
                "SELECT id,status,attempts,error,kind,model_calls,reserved_tokens FROM hq.jobs WHERE id=%s",
                (run_id,),
            )

    @application.post("/v1/plan-runs/{run_id}/cancel")
    def cancel(run_id: UUID, user: Parent):
        with transaction(settings, user) as conn:
            return one(
                conn,
                "UPDATE hq.jobs SET status='cancelled',lease_token=NULL,lease_until=NULL "
                "WHERE id=%s AND status IN ('queued','running') RETURNING id,status",
                (run_id,),
            )

    @application.get("/v1/proposals/{proposal_id}")
    def proposal(proposal_id: UUID, user: Parent):
        with transaction(settings, user) as conn:
            return one(conn, "SELECT * FROM hq.proposals WHERE id=%s", (proposal_id,))

    @application.patch("/v1/proposals/{proposal_id}")
    def patch_proposal(proposal_id: UUID, body: ProposalPatch, user: Parent):
        with transaction(settings, user) as conn:
            proposal = one(
                conn,
                "SELECT *,expires_at<=now() AS expired FROM hq.proposals WHERE id=%s FOR UPDATE",
                (proposal_id,),
            )
            if proposal["confirmed"] or proposal["expired"]:
                raise HTTPException(409, "Proposal is confirmed or expired")
            if proposal["version"] != body.expected_proposal_version:
                raise HTTPException(409, "Proposal version changed; reload before editing")
            release = load_release(conn)
            edit = body.edit.model_dump(mode="json")
            try:
                new_data = apply_patch(proposal["data"], edit, release)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            return one(
                conn,
                "UPDATE hq.proposals SET data=%s,version=version+1 WHERE id=%s RETURNING *",
                (Jsonb(new_data), proposal_id),
            )

    @application.post("/v1/proposals/{proposal_id}/confirm")
    def approve(proposal_id: UUID, body: Confirmation, user: Parent):
        with transaction(settings, user) as conn:
            return confirm(conn, proposal_id, body.expected_plan_version, body.expected_proposal_version)

    @application.get("/v1/children/{child_id}/plan")
    def plan(child_id: UUID, user: Parent):
        with transaction(settings, user) as conn:
            return one(
                conn, "SELECT * FROM hq.plans WHERE child_id=%s ORDER BY version DESC LIMIT 1", (child_id,)
            )

    @application.get("/v1/children/{child_id}/goals")
    def goals(child_id: UUID, user: Parent):
        with transaction(settings, user) as conn:
            one(conn, "SELECT id FROM hq.children WHERE id=%s", (child_id,))
            return conn.execute(
                "SELECT * FROM hq.child_goals WHERE child_id=%s ORDER BY goal_id", (child_id,)
            ).fetchall()

    @application.get("/v1/children/{child_id}/plan-flags")
    def plan_flags_list(child_id: UUID, user: Parent):
        with transaction(settings, user) as conn:
            one(conn, "SELECT id FROM hq.children WHERE id=%s", (child_id,))
            return conn.execute(
                "SELECT f.* FROM hq.plan_flags f "
                "JOIN hq.plans p ON p.id=f.plan_id "
                "WHERE p.child_id=%s AND f.resolved_at IS NULL ORDER BY f.created_at",
                (child_id,),
            ).fetchall()

    @application.post("/v1/plan-flags/{flag_id}/resolve")
    def resolve_flag(flag_id: UUID, user: Parent):
        """Mark a flag reviewed; resolving an already resolved flag returns it unchanged."""
        with transaction(settings, user) as conn:
            resolved = conn.execute(
                "UPDATE hq.plan_flags SET resolved_at=now() WHERE id=%s AND resolved_at IS NULL RETURNING *",
                (flag_id,),
            ).fetchone()
            return resolved or one(conn, "SELECT * FROM hq.plan_flags WHERE id=%s", (flag_id,))

    application.include_router(editorial_router(settings))
    return application


app = create_app()
