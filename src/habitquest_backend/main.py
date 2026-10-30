from typing import Annotated
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.security import OAuth2PasswordRequestForm

from habitquest_backend.auth import TOKEN_URL, bearer_token, dev_login, issue_token, verify_token
from habitquest_backend.config import Settings
from habitquest_backend.db import one, transaction

DESCRIPTION = """
Families register their caregivers and children, describe a child's pain points, and pick habits from a
pre-approved catalog of goals and habits. Kids submit completions, caregivers approve them, and children earn
Sparks (progress, never spent) and points (spent on rewards the caregiver records).

**Authentication.** Every `/v1` endpoint needs a bearer token. Click **Authorize**: when the development login
is enabled, enter username `parent` (or `parent:<label>` for another test parent) and the shared password.
"""
TAGS = [
    {"name": "Health", "description": "Liveness and readiness probes for hosting."},
    {"name": "Auth", "description": "Development login (only when `DEV_LOGIN=true`)."},
    {"name": "Catalog", "description": "Pre-approved goals and the habits parents can pick for each goal."},
]
GOAL_COLUMNS = "id,title,description"
HABIT_COLUMNS = "id,goal_id,title,description,kid_title,kid_hint,icon,anchor,min_age,max_age,tier,points"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    application = FastAPI(
        title="HabitQuest backend",
        version="0.4.0",
        description=DESCRIPTION,
        openapi_tags=TAGS,
    )

    def caller(token: Annotated[str | None, Depends(bearer_token(settings))]) -> UUID:
        if token is None:
            raise HTTPException(401, "Authentication required")
        return verify_token(token, settings)

    User = Annotated[UUID, Depends(caller)]

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
                conn.execute("SELECT 1 FROM hq.families LIMIT 0")
        except psycopg.Error as exc:
            raise HTTPException(503, "Database or migrations unavailable") from exc
        return {"status": "ready"}

    if settings.dev_login:

        @application.post(TOKEN_URL, tags=["Auth"], summary="Development login")
        def login(form: Annotated[OAuth2PasswordRequestForm, Depends()]) -> dict:
            """Exchanges a username and the shared `DEV_LOGIN_PASSWORD` for a 60-minute bearer token.
            Usernames: `parent`, or `parent:<any label>` for a separate parent per label. The user record is
            created on first login. Only exists when `DEV_LOGIN=true`.
            In /docs, use **Authorize** → the password form; Swagger then sends the token for you.
            """
            sub = dev_login(settings, form.username, form.password)
            with transaction(settings, sub) as conn:
                conn.execute(
                    "INSERT INTO hq.users(id,name) VALUES(%s,%s) ON CONFLICT (id) DO NOTHING",
                    (sub, form.username),
                )
            minutes = 60
            return {
                "access_token": issue_token(settings, sub, minutes=minutes),
                "token_type": "bearer",
                "expires_in": minutes * 60,
            }

    @application.get("/v1/goals", tags=["Catalog"], summary="List goals")
    def goals(_: User) -> list[dict]:
        """The active, pre-approved goals (`id`, `title`, `description`), in title order. Pain points are mapped
        to these goals, and every habit belongs to exactly one of them.
        """
        with transaction(settings) as conn:
            return conn.execute(
                f"SELECT {GOAL_COLUMNS} FROM hq.goals WHERE is_active ORDER BY title"
            ).fetchall()

    @application.get("/v1/goals/{goal_id}/habits", tags=["Catalog"], summary="Habits for a goal")
    def goal_habits(
        goal_id: str,
        _: User,
        age: Annotated[int | None, Query(ge=3, le=13, description="Only habits suited to this age")] = None,
        max_tier: Annotated[int | None, Query(ge=1, le=3, description="Only habits up to this tier")] = None,
    ) -> list[dict]:
        """The active habits a parent can pick for one goal, easiest tier first. Each has a parent-facing
        `title` and `description`, kid-facing `kid_title`, `kid_hint` and `icon`, the routine `anchor` it
        belongs to, the age range, its `tier` (1-3, unlocked as the child levels up) and the `points` it pays.
        404 when the goal does not exist or is no longer offered.
        """
        with transaction(settings) as conn:
            one(conn, "SELECT id FROM hq.goals WHERE id=%s AND is_active", (goal_id,))
            return conn.execute(
                f"SELECT {HABIT_COLUMNS} FROM hq.habits WHERE goal_id=%s AND is_active"
                " AND (%s::int IS NULL OR %s::int BETWEEN min_age AND max_age)"
                " AND (%s::int IS NULL OR tier <= %s::int) ORDER BY tier,title",
                (goal_id, age, age, max_tier, max_tier),
            ).fetchall()

    return application


app = create_app()
