"""The quest planner, the weekly review and buddy lines. Each is one model call, not an agent.

The planner and review get facts computed here, under labels instead of names, and their proposed changes are
checked against each child's plan and catalog before they become suggestions. Buddy lines are checked before a
child sees them, and a built-in line replaces any that fails.
"""

import json
import logging
import re
from dataclasses import dataclass
from typing import Annotated, Literal, cast
from uuid import UUID

import openai
from fastapi import APIRouter, Depends, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from habitquest_backend.accounts import StrictModel
from habitquest_backend.auth import KID, signed_in
from habitquest_backend.coach import (
    APP_FACTS,
    LABELS,
    save_suggestion,
    spend_ai_call,
    swap_names,
    take_snapshot,
)
from habitquest_backend.coach_tools import TIME, Snapshot
from habitquest_backend.config import Settings
from habitquest_backend.daily import today_view
from habitquest_backend.db import one, transaction
from habitquest_backend.llm import chat_model
from habitquest_backend.safety import check_text

log = logging.getLogger(__name__)
MAX_PLAN_CHANGES = 4
MAX_REVIEW_CHANGES = 2
MAX_LINE_LENGTH = 160
BUSY = "The AI helpers are busy right now. Please try again in a minute."
PLANNER_PROMPT = f"""You are the quest planner for HabitQuest.
{APP_FACTS}

Given a family snapshot, propose a realistic daily plan for the parent. Each child has their own habits, under a
label such as Child A; keep those labels. Consider each child's age (typical sleep needs are about 10-13 hours at
ages 3-5 and 9-12 hours at ages 6-12), how often each habit was approved recently, their check-ins, and gaps between morning, meals
and bedtime. Prefer small steps: shift a time by 15-30 minutes rather than a big jump, and do not add more than a
family is managing. A child has at most 6 habits. Tie each reason to that child's numbers. If a plan is already
sensible, return no changes. Write for a busy parent: warm, plain and brief. Never give medical advice."""
REVIEWER_PROMPT = f"""You are the weekly reviewer for HabitQuest.
{APP_FACTS}

Review the numbers given, comparing the last 7 days with the 7 before. For each child, name one specific win and one
thing to watch. Point out up to 3 concrete patterns with evidence. Suggest at most 2 changes, only when the numbers
clearly support them. Each child has a label such as Child A; keep those labels. Celebrate effort and never shame a
child. Do not suggest punishments, taking Sparks or points away, or comparing siblings. Write warm, specific and brief."""
BUDDY_PROMPT = """You are a friendly animal buddy in HabitQuest, a children's habit game. You speak one short line to
a child. Rules: one sentence, at most 18 words. Use simple words a child of the given age can read. Be warm, playful
and encouraging. Praise effort, never pressure or guilt. No questions asking for personal information, no real brands,
places or people, nothing scary, no emojis. Output only the line, without quotes.
Events: "greet" = say hello and cheer them toward one of today's quests. "submitted" = the child just tapped "I did
it!" and a grown-up will check soon; praise the effort. "celebrate" = a grown-up approved the quest and they earned a
Spark; celebrate with them."""
BUDDY_FALLBACK = {
    "greet": "Hello, quest hero! Let's do something great today!",
    "submitted": "Great job trying! A grown-up will check soon.",
    "celebrate": "Hooray! You earned a Spark. I'm so proud of you!",
}


class ProposedChange(BaseModel):
    child: str = Field(description="The child's label, e.g. Child A")
    habit_id: str
    action: Literal["add", "retime", "pause", "resume", "remove"]
    cue_time: str = Field(default="", description="24-hour HH:MM, for retime only")
    reason: str = Field(description="One sentence for the parent, tied to the child's numbers")


class PlanAnswer(BaseModel):
    summary: str = Field(description="2-3 warm sentences describing the plan")
    tips: list[str] = Field(default_factory=list, description="Up to 3 short, practical tips")
    changes: list[ProposedChange] = Field(default_factory=list, description="At most 4 changes")


class ChildNote(BaseModel):
    child: str = Field(description="The child's label, e.g. Child A")
    wins: str = Field(description="One specific win from this week")
    watch: str = Field(description="One thing to keep an eye on, or 'Nothing to worry about'")


class ReviewAnswer(BaseModel):
    headline: str = Field(description="One upbeat sentence summing up the week")
    children: list[ChildNote] = Field(default_factory=list)
    patterns: list[str] = Field(default_factory=list, description="Up to 3 patterns with evidence")
    changes: list[ProposedChange] = Field(default_factory=list, description="At most 2 changes")


class BuddyRequest(StrictModel):
    event: Literal["greet", "submitted", "celebrate"]


@dataclass
class FamilyView:
    """Each child under a label, with what the planner and reviewer may see."""

    children: dict[str, dict]  # label -> child row
    snapshots: dict[str, Snapshot]  # label -> snapshot
    weeks: dict[str, dict]  # label -> habit title -> approved days, this week and the week before

    @property
    def restore(self) -> dict[str, str]:
        return {label: child["name"] for label, child in self.children.items()}

    def facts(self) -> str:
        overview = {
            label: {
                "age": snapshot.age,
                "plan": snapshot.plan,
                "approved_days_last_14": snapshot.approved_days,
                "weeks": self.weeks[label],
                "checkins": snapshot.checkins,
                "can_add": [
                    {"habit_id": h["id"], "title": h["title"]}
                    for goal in snapshot.goals
                    for h in goal.habits
                    if h["id"] not in snapshot.plan
                ],
            }
            for label, snapshot in self.snapshots.items()
        }
        return json.dumps(overview)


def weekly_counts(conn, child_id: UUID) -> dict:
    rows = conn.execute(
        "SELECT h.title,count(*) FILTER (WHERE c.day>hq.child_today(%s)-7) AS this_week,"
        "count(*) FILTER (WHERE c.day<=hq.child_today(%s)-7) AS week_before"
        " FROM hq.completions c JOIN hq.child_habits ch ON ch.id=c.child_habit_id JOIN hq.habits h ON h.id=ch.habit_id"
        " WHERE ch.child_id=%s AND c.status='approved' AND c.day>hq.child_today(%s)-14 GROUP BY h.title",
        (child_id, child_id, child_id, child_id),
    ).fetchall()
    return {r["title"]: {"this_week": r["this_week"], "week_before": r["week_before"]} for r in rows}


def family_view(conn, family_id: UUID) -> FamilyView:
    children = conn.execute(
        "SELECT id,family_id,name,age FROM hq.children WHERE family_id=%s ORDER BY created_at,id LIMIT %s",
        (family_id, len(LABELS)),
    ).fetchall()
    if not children:
        raise HTTPException(409, "Add a child first")
    labelled = {f"Child {LABELS[i]}": child for i, child in enumerate(children)}
    return FamilyView(
        labelled,
        {label: take_snapshot(conn, child) for label, child in labelled.items()},
        {label: weekly_counts(conn, child["id"]) for label, child in labelled.items()},
    )


def checked_changes(view: FamilyView, changes: list[ProposedChange], limit: int) -> list[ProposedChange]:
    """Drops anything that cannot apply, and duplicates, so a caregiver only sees cards that can work."""
    kept: dict[tuple[str, str], ProposedChange] = {}
    for change in changes:
        snapshot = view.snapshots.get(change.child)
        if snapshot is None or (change.child, change.habit_id) in kept:
            continue
        available = {h["id"] for goal in snapshot.goals for h in goal.habits}
        usable = (
            change.habit_id in available and change.habit_id not in snapshot.plan
            if change.action == "add"
            else change.habit_id in snapshot.plan
        )
        if usable and (change.action != "retime" or TIME.match(change.cue_time)):
            kept[(change.child, change.habit_id)] = change
    return list(kept.values())[:limit]


def save_changes(conn, view: FamilyView, changes: list[ProposedChange], source: str) -> None:
    for change in changes:
        cue = change.cue_time if change.action == "retime" else None
        reason = swap_names(change.reason, view.restore).strip()[:400] or "This could help."
        save_suggestion(
            conn, view.children[change.child]["id"], source, change.action, change.habit_id, cue, reason
        )


def ask[Answer: BaseModel](settings: Settings, schema: type[Answer], prompt: str, facts: str) -> Answer:
    """One structured model call. Without a model it fails with 503, and nothing is saved."""
    try:
        model = chat_model(settings, temperature=0.1).with_structured_output(schema)
        return cast(Answer, model.invoke([("system", prompt), ("user", f"Family snapshot:\n{facts}")]))
    except (openai.OpenAIError, ValueError) as exc:
        status = exc.status_code if isinstance(exc, openai.APIStatusError) else ""
        log.warning("AI helper failed (%s %s)", type(exc).__name__, status)
        raise HTTPException(503, BUSY) from exc


def plan_for(settings: Settings, view: FamilyView) -> PlanAnswer:
    if settings.ai_mode == "fake":
        return PlanAnswer(summary="Your plans look sensible for now. Keep going with what is working.")
    return ask(settings, PlanAnswer, PLANNER_PROMPT, view.facts())


def review_for(settings: Settings, view: FamilyView) -> ReviewAnswer:
    if settings.ai_mode == "fake":
        notes = [
            ChildNote(
                child=label,
                wins=f"{sum(w['this_week'] for w in view.weeks[label].values())} quests approved this week.",
                watch="Nothing to worry about",
            )
            for label in view.children
        ]
        return ReviewAnswer(headline="Here is how the week went.", children=notes)
    return ask(settings, ReviewAnswer, REVIEWER_PROMPT, view.facts())


def restored_text(view: FamilyView, value):
    """Puts the children's names back into every string of a report."""
    if isinstance(value, str):
        return swap_names(value, view.restore)
    if isinstance(value, list):
        return [restored_text(view, item) for item in value]
    return {key: restored_text(view, item) for key, item in value.items()}


def report_row(conn, family_id: UUID, user: UUID, kind: str, result: dict) -> dict:
    return one(
        conn,
        "INSERT INTO hq.ai_reports(family_id,kind,result,created_by) VALUES(%s,%s,%s,%s)"
        " RETURNING id,kind,result,created_at",
        (family_id, kind, Jsonb(result), user),
    )


def buddy_line(settings: Settings, event: str, age: int, quests: list[str]) -> str:
    """An AI line for the child, or the built-in line for this event if it cannot be made safe."""
    fallback = BUDDY_FALLBACK[event]
    if settings.ai_mode == "fake":
        return fallback
    request = json.dumps({"event": event, "age": age, "quests": quests})
    try:
        reply = chat_model(settings, small=True, temperature=0.8).invoke(
            [("system", BUDDY_PROMPT), ("user", request)]
        )
    except (openai.OpenAIError, ValueError) as exc:
        log.warning("Buddy line failed (%s)", type(exc).__name__)
        return fallback
    line = re.sub(r"\s+", " ", reply.text).strip().strip("\"'“”")
    if not line or len(line) > MAX_LINE_LENGTH:
        return fallback
    return line if check_text(settings, line, "kid-reads").ok else fallback


def router(settings: Settings) -> APIRouter:
    api = APIRouter()
    User = Annotated[UUID, Depends(signed_in(settings))]
    Kid = Annotated[UUID, Depends(signed_in(settings, KID))]

    def run(family_id: UUID, user: UUID, kind: str, source: str, limit: int, build) -> dict:
        with transaction(settings, user) as conn:
            one(conn, "SELECT id FROM hq.families WHERE id=%s", (family_id,))
            spend_ai_call(conn, family_id, settings)
            view = family_view(conn, family_id)
        # The model call happens outside any transaction.
        answer = build(settings, view)
        changes = checked_changes(view, answer.changes, limit)
        fields = answer.model_dump(exclude={"changes"})
        with transaction(settings, user) as conn:
            save_changes(conn, view, changes, source)
            return report_row(conn, family_id, user, kind, restored_text(view, fields))

    @api.post("/v1/families/{family_id}/plan-suggestions", tags=["Coach"], summary="Suggest a plan")
    def suggest_plan(family_id: UUID, user: User) -> dict:
        """The quest planner reads each child's age, plan and recent approvals and writes a `summary` and `tips`.
        Its changes that pass the catalog and plan checks become suggestions from `planner`, at most four.
        Children's names are not sent to the AI provider. 503 when the AI is unavailable.
        """
        return run(family_id, user, "plan", "planner", MAX_PLAN_CHANGES, plan_for)

    @api.post("/v1/families/{family_id}/reviews", tags=["Coach"], summary="Write the weekly review")
    def write_review(family_id: UUID, user: User) -> dict:
        """The reviewer compares the last 7 days with the 7 before and writes a `headline`, each child's `wins`
        and `watch`, and `patterns`. Up to two changes become suggestions from `reviewer`. Children's names are
        not sent to the AI provider. 503 when the AI is unavailable.
        """
        return run(family_id, user, "review", "reviewer", MAX_REVIEW_CHANGES, review_for)

    @api.get("/v1/families/{family_id}/reports", tags=["Coach"], summary="Latest plan and review")
    def latest_reports(family_id: UUID, user: User) -> dict:
        """The newest quest plan and weekly review for the family, each `null` until the first one is written."""
        with transaction(settings, user) as conn:
            one(conn, "SELECT id FROM hq.families WHERE id=%s", (family_id,))
            rows = conn.execute(
                "SELECT DISTINCT ON (kind) id,kind,result,created_at FROM hq.ai_reports WHERE family_id=%s"
                " ORDER BY kind,created_at DESC",
                (family_id,),
            ).fetchall()
        found = {row["kind"]: row for row in rows}
        return {"plan": found.get("plan"), "review": found.get("review")}

    @api.post("/v1/kid/buddy-line", tags=["Kid mode"], summary="A line from the buddy")
    def kid_buddy_line(body: BuddyRequest, kid: Kid) -> dict:
        """With a kid token: one short encouraging line for `greet`, `submitted` or `celebrate`, safety-checked
        before it is returned. A built-in line replaces it when the AI is unavailable or the family's daily AI
        limit is reached. The provider sees the child's age and quest titles, not their name.
        """
        with transaction(settings, kid=kid) as conn:
            child = one(conn, "SELECT age FROM hq.children WHERE id=%s", (kid,))
            view = today_view(conn, kid)
            quests = [h["kid_title"] for h in view["habits"]]
            calls = conn.execute("SELECT hq.kid_ai_call() AS n").fetchone()["n"]
        if calls > settings.ai_daily_limit:
            return {"line": BUDDY_FALLBACK[body.event]}
        return {"line": buddy_line(settings, body.event, child["age"], quests)}

    return api
