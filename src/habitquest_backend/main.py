from typing import Annotated
from uuid import UUID, uuid4

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg.types.json import Jsonb

from habitquest_backend.auth import verify_token
from habitquest_backend.config import Settings
from habitquest_backend.db import one, transaction
from habitquest_backend.schemas import (
    ChildInput,
    Confirmation,
    GoalConfirmation,
    OnboardingInput,
    PreferenceInput,
)
from habitquest_backend.service import confirm, enqueue, questions, session_view


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    application = FastAPI(title="HabitQuest onboarding backend", version="0.1.0")
    bearer = HTTPBearer(auto_error=False)

    def parent(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> UUID:
        if credentials is None:
            raise HTTPException(401, "Parent authentication required")
        return verify_token(credentials.credentials, settings)

    Parent = Annotated[UUID, Depends(parent)]

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
            conn.execute("INSERT INTO hq.households(id) VALUES(%s) ON CONFLICT DO NOTHING", (user,))
            conn.execute("INSERT INTO hq.memberships VALUES(%s,%s) ON CONFLICT DO NOTHING", (user, user))
            return one(
                conn,
                "INSERT INTO hq.children VALUES(%s,%s,%s,%s) RETURNING *",
                (uuid4(), user, body.name, body.age_band),
            )

    @application.post("/v1/children/{child_id}/onboarding", status_code=201)
    def onboard(child_id: UUID, body: OnboardingInput, user: Parent):
        with transaction(settings, user) as conn:
            one(conn, "SELECT id FROM hq.children WHERE id=%s", (child_id,))
            return session_view(
                one(
                    conn,
                    "INSERT INTO hq.sessions(id,child_id,parent_id,data) VALUES(%s,%s,%s,%s) RETURNING *",
                    (uuid4(), child_id, user, Jsonb(body.model_dump())),
                )
            )

    @application.post("/v1/onboarding/{session_id}/clarifications")
    def clarify(session_id: UUID, body: OnboardingInput, user: Parent):
        with transaction(settings, user) as conn:
            return session_view(
                one(
                    conn,
                    "UPDATE hq.sessions SET data=%s,version=version+1,goals_confirmed=false WHERE id=%s RETURNING *",
                    (Jsonb(body.model_dump()), session_id),
                )
            )

    @application.post("/v1/onboarding/{session_id}/confirm-goals")
    def goals(session_id: UUID, body: GoalConfirmation, user: Parent):
        with transaction(settings, user) as conn:
            row = one(conn, "SELECT * FROM hq.sessions WHERE id=%s FOR UPDATE", (session_id,))
            data = {**row["data"], "goals": body.goals}
            if (
                row["version"] != body.expected_version
                or len(set(body.goals)) != len(body.goals)
                or questions(data)
            ):
                raise HTTPException(409, "Stale version, duplicate goals or unanswered questions")
            return session_view(
                one(
                    conn,
                    "UPDATE hq.sessions SET data=%s,goals_confirmed=true,version=version+1 WHERE id=%s RETURNING *",
                    (Jsonb(data), session_id),
                )
            )

    @application.post("/v1/onboarding/{session_id}/plan-runs", status_code=202)
    def start(
        session_id: UUID, user: Parent, idempotency_key: Annotated[str, Header(min_length=1, max_length=100)]
    ):
        with transaction(settings, user) as conn:
            result = enqueue(conn, session_id, user, idempotency_key)
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
                "SELECT id,status,attempts,error,kind,analysis,model_calls,reserved_tokens FROM hq.jobs WHERE id=%s",
                (run_id,),
            )

    @application.post("/v1/onboarding/{session_id}/analyze-runs", status_code=202)
    def analyze(
        session_id: UUID, user: Parent, idempotency_key: Annotated[str, Header(min_length=1, max_length=100)]
    ):
        with transaction(settings, user) as conn:
            result = enqueue(conn, session_id, user, idempotency_key, kind="analysis")
            return {
                "run_id": result["id"],
                "status": result["status"],
                "status_url": f"/v1/plan-runs/{result['id']}",
            }

    @application.post("/v1/plan-runs/{run_id}/cancel")
    def cancel(run_id: UUID, user: Parent):
        with transaction(settings, user) as conn:
            return one(
                conn,
                "UPDATE hq.jobs SET status='cancelled',lease_token=NULL,lease_until=NULL WHERE id=%s AND status IN ('queued','running') RETURNING id,status",
                (run_id,),
            )

    @application.get("/v1/proposals/{proposal_id}")
    def proposal(proposal_id: UUID, user: Parent):
        with transaction(settings, user) as conn:
            return one(conn, "SELECT * FROM hq.proposals WHERE id=%s", (proposal_id,))

    @application.post("/v1/proposals/{proposal_id}/confirm")
    def approve(proposal_id: UUID, body: Confirmation, user: Parent):
        with transaction(settings, user) as conn:
            return confirm(conn, proposal_id, body.expected_plan_version)

    @application.get("/v1/children/{child_id}/plan")
    def plan(child_id: UUID, user: Parent):
        with transaction(settings, user) as conn:
            return one(
                conn, "SELECT * FROM hq.plans WHERE child_id=%s ORDER BY version DESC LIMIT 1", (child_id,)
            )

    @application.get("/v1/children/{child_id}/preferences")
    def preferences(child_id: UUID, user: Parent):
        with transaction(settings, user) as conn:
            one(conn, "SELECT id FROM hq.children WHERE id=%s", (child_id,))
            return conn.execute(
                "SELECT * FROM hq.preferences WHERE child_id=%s ORDER BY kind", (child_id,)
            ).fetchall()

    @application.put("/v1/children/{child_id}/preferences")
    def preference(child_id: UUID, body: PreferenceInput, user: Parent):
        with transaction(settings, user) as conn:
            one(conn, "SELECT id FROM hq.plans WHERE id=%s AND child_id=%s", (body.source_id, child_id))
            return one(
                conn,
                "INSERT INTO hq.preferences(id,child_id,parent_id,kind,value,source_id) VALUES(%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(child_id,kind) DO UPDATE SET value=excluded.value,source_id=excluded.source_id,approved_at=now() RETURNING *",
                (uuid4(), child_id, user, body.kind, Jsonb(body.value), body.source_id),
            )

    @application.delete("/v1/children/{child_id}/preferences/{kind}", status_code=204)
    def delete_preference(child_id: UUID, kind: str, user: Parent):
        with transaction(settings, user) as conn:
            one(conn, "SELECT id FROM hq.children WHERE id=%s", (child_id,))
            conn.execute("DELETE FROM hq.preferences WHERE child_id=%s AND kind=%s", (child_id, kind))

    return application


app = create_app()
