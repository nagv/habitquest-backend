import threading
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID, uuid4

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg.types.json import Jsonb

from habitquest_backend.auth import verify_token
from habitquest_backend.catalog.models import profile_from_row
from habitquest_backend.catalog.repository import load_release
from habitquest_backend.config import Settings
from habitquest_backend.db import one, transaction
from habitquest_backend.editorial.api import router as editorial_router
from habitquest_backend.schemas import ChildInput, Confirmation, IntakeInput, ProfileUpdate, ProposalPatch
from habitquest_backend.service import apply_patch, confirm, cue_loss_habits, enqueue, flag_plan
from habitquest_backend.worker import serve

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


DESCRIPTION = """
Parents describe a child's pain points; the backend suggests reviewed habits from a versioned catalog for
the parent to edit and confirm. Catalog staff maintain that catalog through a two-person review and release
process.

**Authentication.** Every `/v1` endpoint needs a bearer token. Parent routes accept any authenticated user;
`/v1/editor` routes also need `app_metadata.hq_role = "editor"`. Click **Authorize** and paste the token
(locally: `uv run python scripts/local_token.py`, with `--editor` or `--reviewer` for editors).

**Parent flow.** Onboard a child → describe pain points (intake) → start planning → poll the run → review
and optionally edit the proposal → confirm it as a plan. Repeat with a new intake for new pain points.

**Editor flow.** Draft → submit (checks and lint) → approve by a second editor → build a release → diff and
validate → publish by a second editor. Withdraw a habit in an emergency.
"""
TAGS = [
    {"name": "Health", "description": "Liveness and readiness probes for hosting."},
    {
        "name": "Children",
        "description": "Child profiles: durable facts and the routine schedule habits are tied to.",
    },
    {
        "name": "Planning",
        "description": "Pain-point intakes and the planning job that turns them into a proposal.",
    },
    {
        "name": "Proposals",
        "description": "The parent review screen: read, edit within the rules, and confirm.",
    },
    {
        "name": "Plans",
        "description": "Confirmed plans, goal history and flags that need the parent's review.",
    },
    {"name": "Editor: catalog", "description": "Catalog drafts and their two-person review (editor role)."},
    {
        "name": "Editor: releases",
        "description": "Building, checking and publishing catalog releases (editor role).",
    },
]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # Single-service hosting: the planning worker polls on a thread beside the API.
        stop = threading.Event()
        thread = threading.Thread(target=serve, args=(settings, stop), name="plan-worker", daemon=True)
        if settings.run_worker_in_api:
            thread.start()
        yield
        stop.set()
        if thread.is_alive():
            thread.join(timeout=settings.max_run_seconds)

    application = FastAPI(
        title="HabitQuest onboarding backend",
        version="0.3.0",
        description=DESCRIPTION,
        openapi_tags=TAGS,
        lifespan=lifespan,
    )
    bearer = HTTPBearer(auto_error=False)

    def parent(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> UUID:
        if credentials is None:
            raise HTTPException(401, "Parent authentication required")
        return verify_token(credentials.credentials, settings)

    Parent = Annotated[UUID, Depends(parent)]
    Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=100)]

    @application.get("/health/live", tags=["Health"], summary="Liveness probe")
    def live() -> dict[str, str]:
        """Returns `ok` while the process runs; does not touch the database. Use it for uptime checks."""
        return {"status": "ok"}

    @application.get("/health/ready", tags=["Health"], summary="Readiness probe")
    def ready():
        """Checks the database connection and that migrations are applied; 503 otherwise.
        Use it as the hosting health check (Render: `/health/ready`).
        """
        try:
            with transaction(settings) as conn:
                conn.execute("SELECT 1 FROM hq.children LIMIT 0")
        except psycopg.Error as exc:
            raise HTTPException(503, "Database or migrations unavailable") from exc
        return {"status": "ready"}

    @application.post("/v1/children", status_code=201, tags=["Children"], summary="Onboard a child")
    def add_child(body: ChildInput, user: Parent):
        """Creates a child profile in the calling parent's household (created on first use): nickname, age band,
        interests, accessibility needs, opted-in topics, preferred world, household timezone and the routine
        schedule, a cue time and days per anchor (`morning`, `after_school`, `evening`, `bedtime`).
        The schedule decides which habits can be scheduled: a habit needs a cue for its anchor.
        """
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

    @application.get("/v1/children/{child_id}", tags=["Children"], summary="Read a child profile")
    def get_child(child_id: UUID, user: Parent):
        """Returns the profile with its `profile_version`, which an edit must send back."""
        with transaction(settings, user) as conn:
            return child_view(conn, child_id)

    @application.patch("/v1/children/{child_id}", tags=["Children"], summary="Edit a child profile")
    def update_child(child_id: UUID, body: ProfileUpdate, user: Parent):
        """Replaces the profile; send `expected_profile_version` (409 if it changed meanwhile).
        Never changes a confirmed plan. If a removed routine cue leaves an active habit without one, the plan gets
        a `cue_removed` flag. Open proposals built on the old profile can no longer be edited or confirmed.
        """
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

    @application.post(
        "/v1/children/{child_id}/intakes", status_code=201, tags=["Planning"], summary="Describe pain points"
    )
    def add_intake(child_id: UUID, body: IntakeInput, user: Parent):
        """Saves one round of the parent's concerns, in their own words, with `available_minutes`: the child's
        daily total, including habits already in the plan. Add a new intake whenever new pain points come up;
        each round plans against the current confirmed plan. `store_feedback: true` lets anonymised phrases that
        match no catalog goal be kept for catalog editors.
        """
        with transaction(settings, user) as conn:
            one(conn, "SELECT id FROM hq.children WHERE id=%s", (child_id,))
            return one(
                conn,
                "INSERT INTO hq.intakes(id,child_id,parent_id,data) VALUES(%s,%s,%s,%s) RETURNING *",
                (uuid4(), child_id, user, Jsonb(body.model_dump())),
            )

    @application.post(
        "/v1/intakes/{intake_id}/plan-runs", status_code=202, tags=["Planning"], summary="Start planning"
    )
    def start(intake_id: UUID, user: Parent, idempotency_key: Key):
        """Queues the planning job for an intake. Requires an `Idempotency-Key` header: retrying with the same key
        returns the same run. The worker maps the concerns to catalog goals, ranks the eligible reviewed habits,
        lets the agents pick within code-enforced limits, and has the Reviewer check the result.
        Returns 202 with the run id and a status URL.
        """
        with transaction(settings, user) as conn:
            result = enqueue(conn, intake_id, user, idempotency_key)
            return {
                "run_id": result["id"],
                "status": result["status"],
                "status_url": f"/v1/plan-runs/{result['id']}",
            }

    @application.get("/v1/plan-runs/{run_id}", tags=["Planning"], summary="Planning status")
    def status(run_id: UUID, user: Parent):
        """Poll until `status` is `ready` (a proposal exists), `failed` (see `error`) or `cancelled`.
        Also reports the run's model calls and reserved request units.
        """
        with transaction(settings, user) as conn:
            return one(
                conn,
                "SELECT id,status,attempts,error,kind,model_calls,reserved_tokens FROM hq.jobs WHERE id=%s",
                (run_id,),
            )

    @application.post("/v1/plan-runs/{run_id}/cancel", tags=["Planning"], summary="Cancel planning")
    def cancel(run_id: UUID, user: Parent):
        """Cancels a queued or running job; a worker stops at its next check and publishes nothing."""
        with transaction(settings, user) as conn:
            return one(
                conn,
                "UPDATE hq.jobs SET status='cancelled',lease_token=NULL,lease_until=NULL "
                "WHERE id=%s AND status IN ('queued','running') RETURNING id,status",
                (run_id,),
            )

    @application.get("/v1/proposals/{proposal_id}", tags=["Proposals"], summary="Review a proposal")
    def proposal(proposal_id: UUID, user: Parent):
        """The parent review screen; the proposal id is the run id. Contains the mapped goals with the quoted
        evidence and a status (`covered`, `needs_cue`, `deferred`, `no_catalog_entry`, `paused`), active habits
        with level, minutes, cue and days, future candidates, per-goal shortlists with scores, the chapter
        outline, questions for the parent, rationale, the review result, and `fallback` (true when the reviewed
        template plan was used instead of the agents' picks).
        """
        with transaction(settings, user) as conn:
            return one(conn, "SELECT * FROM hq.proposals WHERE id=%s", (proposal_id,))

    @application.patch("/v1/proposals/{proposal_id}", tags=["Proposals"], summary="Edit a proposal")
    def patch_proposal(proposal_id: UUID, body: ProposalPatch, user: Parent):
        """Applies one edit with `expected_proposal_version`: `remove_goal`, `reorder_goals`, `change_level`,
        `change_schedule`, `swap_habit`, `demote_active` or `promote_future`. Each edit is rebuilt from the
        reviewed catalog and re-checked: no more than the allowed active habits, minutes within the daily total,
        a cue for every habit, and no covered goal left uncovered. A violation returns 422 whose message starts
        with the rule's name; a changed profile or proposal version returns 409.
        """
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
            child = one(conn, "SELECT * FROM hq.children WHERE id=%s", (proposal["child_id"],))
            if proposal["data"]["profile_version"] != child["profile_version"]:
                raise HTTPException(409, "Profile changed; request a new proposal")
            intake = one(
                conn,
                "SELECT i.data FROM hq.jobs j JOIN hq.intakes i ON i.id=j.intake_id WHERE j.id=%s",
                (proposal_id,),
            )
            policy = one(conn, "SELECT max_active_habits FROM hq.policy WHERE name='onboarding'")
            try:
                new_data = apply_patch(
                    proposal["data"],
                    body.edit.model_dump(mode="json"),
                    load_release(conn),
                    profile_from_row(child),
                    max_active=policy["max_active_habits"],
                    available_minutes=intake["data"]["available_minutes"],
                    plan_version=proposal["base_plan_version"] + 1,
                )
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            return one(
                conn,
                "UPDATE hq.proposals SET data=%s,version=version+1 WHERE id=%s RETURNING *",
                (Jsonb(new_data), proposal_id),
            )

    @application.post("/v1/proposals/{proposal_id}/confirm", tags=["Proposals"], summary="Confirm a proposal")
    def approve(proposal_id: UUID, body: Confirmation, user: Parent):
        """Turns the proposal into the child's next immutable plan version. Send `expected_plan_version` (the
        current version, 0 for the first plan) and `expected_proposal_version`. Every habit and chapter is
        revalidated against the current catalog release (409 `replan_required` if one was withdrawn or
        replaced); goals are recorded and flags on superseded plans are resolved. Idempotent.
        """
        with transaction(settings, user) as conn:
            return confirm(conn, proposal_id, body.expected_plan_version, body.expected_proposal_version)

    @application.get("/v1/children/{child_id}/plan", tags=["Plans"], summary="Current plan")
    def plan(child_id: UUID, user: Parent):
        """The child's latest confirmed plan version: active habits with cue and days, chapters and goals."""
        with transaction(settings, user) as conn:
            return one(
                conn, "SELECT * FROM hq.plans WHERE child_id=%s ORDER BY version DESC LIMIT 1", (child_id,)
            )

    @application.get("/v1/children/{child_id}/goals", tags=["Plans"], summary="Goal history")
    def goals(child_id: UUID, user: Parent):
        """The child's goals with status (`active`, `paused`, `resolved`), the plan version they started in and the intake that raised them."""
        with transaction(settings, user) as conn:
            one(conn, "SELECT id FROM hq.children WHERE id=%s", (child_id,))
            return conn.execute(
                "SELECT * FROM hq.child_goals WHERE child_id=%s ORDER BY goal_id", (child_id,)
            ).fetchall()

    @application.get("/v1/children/{child_id}/plan-flags", tags=["Plans"], summary="Open plan flags")
    def plan_flags_list(child_id: UUID, user: Parent):
        """Issues on confirmed plans that need the parent's review: a habit withdrawn from the catalog
        (`habit_withdrawn`) or a routine cue removed from the profile (`cue_removed`). Unresolved flags only.
        """
        with transaction(settings, user) as conn:
            one(conn, "SELECT id FROM hq.children WHERE id=%s", (child_id,))
            return conn.execute(
                "SELECT f.* FROM hq.plan_flags f "
                "JOIN hq.plans p ON p.id=f.plan_id "
                "WHERE p.child_id=%s AND f.resolved_at IS NULL ORDER BY f.created_at",
                (child_id,),
            ).fetchall()

    @application.post("/v1/plan-flags/{flag_id}/resolve", tags=["Plans"], summary="Resolve a plan flag")
    def resolve_flag(flag_id: UUID, user: Parent):
        """Marks a flag reviewed; resolving it again returns it unchanged. Confirming a newer plan also resolves
        the flags on the plans it replaces.
        """
        with transaction(settings, user) as conn:
            resolved = conn.execute(
                "UPDATE hq.plan_flags SET resolved_at=now() WHERE id=%s AND resolved_at IS NULL RETURNING *",
                (flag_id,),
            ).fetchone()
            return resolved or one(conn, "SELECT * FROM hq.plan_flags WHERE id=%s", (flag_id,))

    application.include_router(editorial_router(settings))
    return application


app = create_app()
