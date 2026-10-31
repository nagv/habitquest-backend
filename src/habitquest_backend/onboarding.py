"""Children, pain points mapped to goals, and each child's plan (the habits they are working on)."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from psycopg.types.json import Jsonb
from pydantic import Field, field_validator

from habitquest_backend.accounts import StrictModel
from habitquest_backend.ai import Candidate, map_pain_point
from habitquest_backend.auth import signed_in
from habitquest_backend.config import Settings
from habitquest_backend.db import one, transaction

MAX_ACTIVE_HABITS = 6
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DEFAULT_CUES = {
    "morning": "07:00",
    "midday": "12:30",
    "after_school": "16:00",
    "evening": "18:30",
    "bedtime": "20:00",
}
# (Sparks needed, level, name, highest habit tier unlocked), as in the frontend.
LEVELS = [
    (0, 1, "Sprout", 1),
    (5, 2, "Explorer", 2),
    (15, 3, "Adventurer", 3),
    (30, 4, "Hero", 3),
    (50, 5, "Legend", 3),
]
CHILD_COLUMNS = "id,family_id,name,age,buddy,world,routine_times,created_at"
HABIT_COLUMNS = "h.id,h.goal_id,h.title,h.description,h.kid_title,h.kid_hint,h.icon,h.anchor,h.tier,h.points"

Anchor = Literal["morning", "midday", "after_school", "evening", "bedtime"]
Day = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
Time = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$", description="24-hour HH:MM")]
Buddy = Literal["fox", "owl", "bear", "bunny"]
World = Literal["camp", "mosswood", "sky"]
Days = Annotated[list[Day], Field(min_length=1, max_length=7)]


def unique(values: list | None) -> list | None:
    if values is not None and len(set(values)) != len(values):
        raise ValueError("List items must be unique")
    return values


class ChildInput(StrictModel):
    name: str = Field(min_length=1, max_length=40)
    age: int = Field(ge=3, le=13)
    buddy: Buddy = "fox"
    world: World = "camp"
    routine_times: dict[Anchor, Time] = Field(
        default_factory=dict, description='When each part of the day starts, e.g. {"bedtime": "20:00"}'
    )


class ChildUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=40)
    age: int | None = Field(default=None, ge=3, le=13)
    buddy: Buddy | None = None
    world: World | None = None
    routine_times: dict[Anchor, Time] | None = None


class PainPointInput(StrictModel):
    text: str = Field(min_length=5, max_length=2000, description="The problem, in the parent's own words")


class PlanHabit(StrictModel):
    habit_id: str
    cue_time: Time | None = None
    days: Days | None = None

    _days = field_validator("days")(unique)


class PlanInput(StrictModel):
    goals: list[str] = Field(max_length=7, description="Goal ids to work on; others are dropped")
    habits: list[PlanHabit] = Field(max_length=MAX_ACTIVE_HABITS)

    _goals = field_validator("goals")(unique)

    @field_validator("habits")
    @classmethod
    def unique_habits(cls, habits: list[PlanHabit]) -> list[PlanHabit]:
        unique([h.habit_id for h in habits])
        return habits


class ChildHabitUpdate(StrictModel):
    cue_time: Time | None = None
    days: Days | None = None
    status: Literal["active", "paused", "removed"] | None = None

    _days = field_validator("days")(unique)


def progress(conn, child_id: UUID) -> dict:
    """Sparks (approved completions), the level they reach, and the highest habit tier it unlocks."""
    sparks = conn.execute(
        "SELECT count(*) AS n FROM hq.completions c JOIN hq.child_habits ch ON ch.id=c.child_habit_id"
        " WHERE ch.child_id=%s AND c.status='approved'",
        (child_id,),
    ).fetchone()["n"]
    _, level, name, tier = [entry for entry in LEVELS if sparks >= entry[0]][-1]
    following = [entry[0] for entry in LEVELS if entry[0] > sparks]
    return {
        "sparks": sparks,
        "level": level,
        "level_name": name,
        "unlocked_tier": tier,
        "next_level_at": following[0] if following else None,
    }


def candidates(conn, age: int, tier: int) -> list[Candidate]:
    """Active goals, each with the active habits suited to this age and unlocked tier (goals with none left out)."""
    goals = conn.execute(
        "SELECT id,title,description,keywords FROM hq.goals WHERE is_active ORDER BY title"
    ).fetchall()
    habits = conn.execute(
        "SELECT id,goal_id,title,tier FROM hq.habits WHERE is_active AND %s BETWEEN min_age AND max_age"
        " AND tier<=%s ORDER BY tier,title",
        (age, tier),
    ).fetchall()
    found = [
        Candidate(
            g["id"],
            g["title"],
            g["description"],
            g["keywords"],
            [
                {"id": h["id"], "title": h["title"], "tier": h["tier"]}
                for h in habits
                if h["goal_id"] == g["id"]
            ],
        )
        for g in goals
    ]
    return [c for c in found if c.habits]


def plan_view(conn, child_id: UUID) -> dict:
    child = one(conn, "SELECT id,age FROM hq.children WHERE id=%s", (child_id,))
    level = progress(conn, child_id)
    goals = conn.execute(
        "SELECT cg.goal_id,g.title,cg.status,cg.reason,cg.recommended_habit_ids FROM hq.child_goals cg"
        " JOIN hq.goals g ON g.id=cg.goal_id WHERE cg.child_id=%s AND cg.status IN ('suggested','active')"
        " ORDER BY cg.created_at,cg.goal_id",
        (child_id,),
    ).fetchall()
    options = conn.execute(
        f"SELECT {HABIT_COLUMNS} FROM hq.habits h WHERE h.is_active AND h.goal_id=ANY(%s)"
        " AND %s BETWEEN h.min_age AND h.max_age AND h.tier<=%s ORDER BY h.tier,h.title",
        ([g["goal_id"] for g in goals], child["age"], level["unlocked_tier"]),
    ).fetchall()
    for goal in goals:
        goal["options"] = [
            {**h, "recommended": h["id"] in goal["recommended_habit_ids"]}
            for h in options
            if h["goal_id"] == goal["goal_id"]
        ]
    habits = conn.execute(
        f"SELECT ch.id,ch.habit_id,{HABIT_COLUMNS.replace('h.id,', '')},to_char(ch.cue_time,'HH24:MI') AS cue_time,"
        "ch.days,ch.status,ch.started_on FROM hq.child_habits ch JOIN hq.habits h ON h.id=ch.habit_id"
        " WHERE ch.child_id=%s AND ch.status<>'removed' ORDER BY ch.cue_time,h.title",
        (child_id,),
    ).fetchall()
    return {"child_id": child["id"], "progress": level, "goals": goals, "habits": habits}


def router(settings: Settings) -> APIRouter:
    api = APIRouter()
    User = Annotated[UUID, Depends(signed_in(settings))]

    @api.post("/v1/families/{family_id}/children", status_code=201, tags=["Children"], summary="Add a child")
    def add_child(family_id: UUID, body: ChildInput, user: User) -> dict:
        """Adds a child to the family: `name`, `age` (3-13, decides which habits suit them), `buddy` (fox, owl,
        bear, bunny), `world` (camp, mosswood, sky) and optional `routine_times` per part of the day
        (`morning`, `midday`, `after_school`, `evening`, `bedtime`), used as default habit times.
        """
        with transaction(settings, user) as conn:
            one(conn, "SELECT id FROM hq.families WHERE id=%s", (family_id,))
            return one(
                conn,
                f"INSERT INTO hq.children(family_id,name,age,buddy,world,routine_times) VALUES(%s,%s,%s,%s,%s,%s)"
                f" RETURNING {CHILD_COLUMNS}",
                (family_id, body.name, body.age, body.buddy, body.world, Jsonb(body.routine_times)),
            )

    @api.get("/v1/families/{family_id}/children", tags=["Children"], summary="List children")
    def list_children(family_id: UUID, user: User) -> list[dict]:
        """The family's children, oldest record first. 404 unless the caller cares for this family."""
        with transaction(settings, user) as conn:
            one(conn, "SELECT id FROM hq.families WHERE id=%s", (family_id,))
            return conn.execute(
                f"SELECT {CHILD_COLUMNS} FROM hq.children WHERE family_id=%s ORDER BY created_at,id",
                (family_id,),
            ).fetchall()

    @api.get("/v1/children/{child_id}", tags=["Children"], summary="Read a child")
    def read_child(child_id: UUID, user: User) -> dict:
        """One child's profile. 404 unless the caller cares for the child's family."""
        with transaction(settings, user) as conn:
            return one(conn, f"SELECT {CHILD_COLUMNS} FROM hq.children WHERE id=%s", (child_id,))

    @api.patch("/v1/children/{child_id}", tags=["Children"], summary="Edit a child")
    def update_child(child_id: UUID, body: ChildUpdate, user: User) -> dict:
        """Changes any of `name`, `age`, `buddy`, `world` and `routine_times` (replaced as a whole); omitted
        fields stay as they are. Habits already in the plan keep their times.
        """
        changes = body.model_dump(exclude_none=True)
        with transaction(settings, user) as conn:
            one(conn, "SELECT id FROM hq.children WHERE id=%s", (child_id,))
            for column, value in changes.items():
                value = Jsonb(value) if column == "routine_times" else value
                conn.execute(f"UPDATE hq.children SET {column}=%s WHERE id=%s", (value, child_id))
            return one(conn, f"SELECT {CHILD_COLUMNS} FROM hq.children WHERE id=%s", (child_id,))

    @api.post(
        "/v1/children/{child_id}/pain-points",
        status_code=201,
        tags=["Onboarding"],
        summary="Describe a problem",
    )
    def add_pain_point(child_id: UUID, body: PainPointInput, user: User, response: Response) -> dict:
        """The parent describes a problem in their own words. AI maps it to one to three pre-approved goals, each
        with a one-sentence `reason`, and recommends up to two habits per goal from those suited to the child's
        age and unlocked tier. The goals are saved as `suggested` (goals already active stay active) and the
        response is the child's plan, where each goal lists its habit `options` with `recommended` flags.

        `mapped_by` says how: `ai`; `keywords` when the AI was unavailable (no recommendations); `manual` when
        nothing matched, and the parent picks goals from `GET /v1/goals`. Only the child's age and this text
        are sent to the AI provider. If the text suggests a child may be at risk, nothing is saved and the
        response (200) carries `safety` guidance instead.
        """
        with transaction(settings, user) as conn:
            child = one(conn, "SELECT id,age FROM hq.children WHERE id=%s", (child_id,))
            options = candidates(conn, child["age"], progress(conn, child_id)["unlocked_tier"])
        # The model call happens outside any transaction.
        mapping = map_pain_point(settings, child["age"], body.text, options)
        if mapping.safety:
            response.status_code = 200
            return {"pain_point": None, "safety": {"guidance": mapping.safety}, "plan": None}
        with transaction(settings, user) as conn:
            point = one(
                conn,
                "INSERT INTO hq.pain_points(child_id,text,mapped_by,created_by) VALUES(%s,%s,%s,%s)"
                " RETURNING id,text,mapped_by,created_at",
                (child_id, body.text, mapping.mapped_by, user),
            )
            for goal_id, reason in mapping.goals:
                conn.execute(
                    "INSERT INTO hq.child_goals(child_id,goal_id,pain_point_id,reason,recommended_habit_ids)"
                    " VALUES(%s,%s,%s,%s,%s) ON CONFLICT (child_id,goal_id) DO UPDATE SET"
                    " pain_point_id=EXCLUDED.pain_point_id,reason=EXCLUDED.reason,"
                    "recommended_habit_ids=EXCLUDED.recommended_habit_ids,"
                    "status=CASE WHEN hq.child_goals.status='active' THEN 'active' ELSE 'suggested' END",
                    (child_id, goal_id, point["id"], reason, mapping.recommended.get(goal_id, [])),
                )
            return {"pain_point": point, "safety": None, "plan": plan_view(conn, child_id)}

    @api.get("/v1/children/{child_id}/plan", tags=["Onboarding"], summary="Read the plan")
    def read_plan(child_id: UUID, user: User) -> dict:
        """The child's `progress` (Sparks, level, unlocked tier), their suggested and active `goals` (each with
        the habit `options` the parent can pick, recommended ones flagged) and their current `habits` with cue
        time, days and status (`active` or `paused`).
        """
        with transaction(settings, user) as conn:
            return plan_view(conn, child_id)

    @api.put("/v1/children/{child_id}/plan", tags=["Onboarding"], summary="Set the plan")
    def set_plan(child_id: UUID, body: PlanInput, user: User) -> dict:
        """Sets the child's plan in one step: the `goals` to work on and the `habits` to do, at most six.
        Each habit must belong to one of the listed goals and suit the child's age and unlocked tier (422
        otherwise). `cue_time` defaults to the habit's current time, else the child's routine time for that
        part of the day, else a standard time; `days` defaults to every day. Listed goals become `active`
        and other suggested or active goals `dropped`; habits not listed are removed (kept as history).
        Use this both to activate the first plan and to change it later.
        """
        requested = [h.habit_id for h in body.habits]
        with transaction(settings, user) as conn:
            child = one(conn, "SELECT id,age,routine_times FROM hq.children WHERE id=%s", (child_id,))
            tier = progress(conn, child_id)["unlocked_tier"]
            goals = {
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM hq.goals WHERE is_active AND id=ANY(%s)", (body.goals,)
                ).fetchall()
            }
            if unknown := [g for g in body.goals if g not in goals]:
                raise HTTPException(422, f"Unknown goal: {unknown[0]}")
            catalog = {
                row["id"]: row
                for row in conn.execute(
                    "SELECT id,goal_id,anchor,min_age,max_age,tier FROM hq.habits WHERE is_active AND id=ANY(%s)",
                    (requested,),
                ).fetchall()
            }
            for habit_id in requested:
                habit = catalog.get(habit_id)
                if habit is None:
                    raise HTTPException(422, f"Unknown habit: {habit_id}")
                if habit["goal_id"] not in goals:
                    raise HTTPException(
                        422, f"{habit_id} belongs to goal {habit['goal_id']}, which is not listed"
                    )
                if not habit["min_age"] <= child["age"] <= habit["max_age"]:
                    raise HTTPException(422, f"{habit_id} is for ages {habit['min_age']}-{habit['max_age']}")
                if habit["tier"] > tier:
                    raise HTTPException(
                        422, f"{habit_id} is tier {habit['tier']}; this child has unlocked tier {tier}"
                    )
            conn.execute(
                "UPDATE hq.child_goals SET status='dropped' WHERE child_id=%s AND status IN ('suggested','active')"
                " AND NOT goal_id=ANY(%s)",
                (child_id, body.goals),
            )
            for goal_id in body.goals:
                conn.execute(
                    "INSERT INTO hq.child_goals(child_id,goal_id,status) VALUES(%s,%s,'active')"
                    " ON CONFLICT (child_id,goal_id) DO UPDATE SET status='active'",
                    (child_id, goal_id),
                )
            live = {
                row["habit_id"]: row
                for row in conn.execute(
                    "SELECT id,habit_id,to_char(cue_time,'HH24:MI') AS cue_time,days FROM hq.child_habits"
                    " WHERE child_id=%s AND status<>'removed'",
                    (child_id,),
                ).fetchall()
            }
            conn.execute(
                "UPDATE hq.child_habits SET status='removed',ended_on=current_date"
                " WHERE child_id=%s AND status<>'removed' AND NOT habit_id=ANY(%s)",
                (child_id, requested),
            )
            for wanted in body.habits:
                current = live.get(wanted.habit_id)
                anchor = catalog[wanted.habit_id]["anchor"]
                cue = (
                    wanted.cue_time
                    or (current and current["cue_time"])
                    or child["routine_times"].get(anchor)
                    or DEFAULT_CUES[anchor]
                )
                days = wanted.days or (current and current["days"]) or DAYS
                if current:
                    conn.execute(
                        "UPDATE hq.child_habits SET cue_time=%s,days=%s,status='active' WHERE id=%s",
                        (cue, days, current["id"]),
                    )
                else:
                    conn.execute(
                        "INSERT INTO hq.child_habits(child_id,habit_id,cue_time,days) VALUES(%s,%s,%s,%s)",
                        (child_id, wanted.habit_id, cue, days),
                    )
            return plan_view(conn, child_id)

    @api.patch("/v1/child-habits/{child_habit_id}", tags=["Onboarding"], summary="Adjust one habit")
    def update_child_habit(child_habit_id: UUID, body: ChildHabitUpdate, user: User) -> dict:
        """Changes one habit in a child's plan: `cue_time`, `days`, or `status` (`active`, `paused`, or
        `removed`, which ends it and keeps it as history). A removed habit cannot be changed (409); pick it
        again through the plan. Returns the child's plan.
        """
        changes = body.model_dump(exclude_none=True)
        with transaction(settings, user) as conn:
            row = one(conn, "SELECT child_id,status FROM hq.child_habits WHERE id=%s", (child_habit_id,))
            if row["status"] == "removed":
                raise HTTPException(409, "This habit was removed; add it again through the plan")
            for column, value in changes.items():
                conn.execute(f"UPDATE hq.child_habits SET {column}=%s WHERE id=%s", (value, child_habit_id))
            if changes.get("status") == "removed":
                conn.execute(
                    "UPDATE hq.child_habits SET ended_on=current_date WHERE id=%s", (child_habit_id,)
                )
            return plan_view(conn, row["child_id"])

    return api
