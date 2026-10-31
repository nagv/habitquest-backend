"""The coach's tools. They work on a snapshot in memory, so they can neither reach the database nor change
anything: what the coach wants to do is collected in `Turn`, and a caregiver approves it later.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Literal

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field

from habitquest_backend.ai import Candidate
from habitquest_backend.guidance import search_guidance

MAX_RECOMMENDATIONS = 3
REASON_LENGTH = 240
Action = Literal["retime", "pause", "resume", "remove"]
TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@dataclass
class Snapshot:
    """What one turn may see: never a name from our records, only the age, tier, catalog and progress."""

    age: int
    goals: list[Candidate]  # already filtered to this child's age and unlocked tier
    plan: dict[str, dict]  # habit_id -> title, cue_time, status
    approved_days: dict[str, int]  # habit_id -> approved days in the last 14
    shown: set[str] = field(default_factory=set)  # habits already shown or rejected in this conversation


@dataclass
class Change:
    habit_id: str
    kind: Action
    cue_time: str | None
    reason: str


@dataclass
class Turn:
    """What the coach collected during one turn."""

    recommendations: dict[str, str] = field(default_factory=dict)  # habit_id -> reason
    changes: list[Change] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)  # ref -> title


class Pick(BaseModel):
    habit_id: str
    reason: str = Field(description="One sentence linking this habit to the parent's problem")


def build_tools(snapshot: Snapshot, turn: Turn) -> list[BaseTool]:
    titles = {h["id"]: h["title"] for goal in snapshot.goals for h in goal.habits}

    @tool
    def get_progress() -> str:
        """Current habits with their time and status, plus how many of the last 14 days each was approved."""
        plan = [
            {**habit, "habit_id": habit_id, "approved_days": snapshot.approved_days.get(habit_id, 0)}
            for habit_id, habit in snapshot.plan.items()
        ]
        return json.dumps({"days": 14, "plan": plan})

    @tool
    def research(problem: str) -> str:
        """Look up help for this child's problem: guidance passages with refs to cite like [G12], plus every
        habit this child can start now, grouped by goal. Habits already in the plan or shown are left out.

        Args:
            problem: The problem in the parent's words plus key topics, e.g. "meltdowns when screen time ends"
        """
        chunks = search_guidance(problem, snapshot.age)
        turn.sources |= {c.ref: c.title for c in chunks}
        taken = snapshot.shown | snapshot.plan.keys()
        goals = [
            {
                "goal": goal.title,
                "habits": [
                    {"habit_id": h["id"], "title": h["title"], "tier": h["tier"]}
                    for h in goal.habits
                    if h["id"] not in taken
                ],
            }
            for goal in snapshot.goals
        ]
        return json.dumps(
            {
                "sources": [{"ref": c.ref, "title": c.title, "text": c.text} for c in chunks],
                **(
                    {}
                    if chunks
                    else {"note": "No matching guidance. Use cautious general principles and say so."}
                ),
                "goals": [g for g in goals if g["habits"]],
            }
        )

    @tool
    def recommend_habits(quests: list[Pick]) -> str:
        """Show the parent up to 3 habits as selectable cards, replacing earlier cards this turn. Use only habit
        ids from research. This is the only way habits appear as cards. The parent decides, with their PIN.
        """
        kept, errors = {}, []
        for pick in quests[:MAX_RECOMMENDATIONS]:
            if pick.habit_id not in titles:
                errors.append(f"{pick.habit_id}: not available for this child")
            elif pick.habit_id in snapshot.plan:
                errors.append(f"{pick.habit_id}: already in the plan")
            elif pick.habit_id in snapshot.shown:
                errors.append(f"{pick.habit_id}: already shown; pick something different")
            else:
                kept[pick.habit_id] = pick.reason.strip()[:REASON_LENGTH] or "This could help."
        if not kept:
            return f"Error: no valid habits. {'; '.join(errors)}. Use habit ids from research."
        turn.recommendations = kept
        skipped = f" Skipped: {'; '.join(errors)}." if errors else ""
        return (
            f"Shown {len(kept)} card(s) to the parent.{skipped} They are added only if the parent picks them."
        )

    @tool
    def propose_plan_change(habit_id: str, action: Action, reason: str, cue_time: str = "") -> str:
        """Propose changing a habit already in the plan. This changes nothing: the parent sees a card and
        approves it with their PIN. Make one call per habit.

        Args:
            habit_id: A habit id from get_progress
            action: retime, pause, resume or remove
            reason: One sentence for the parent explaining why
            cue_time: The new 24-hour HH:MM time, for retime only
        """
        if habit_id not in snapshot.plan:
            return f"Error: {habit_id} is not in this child's plan."
        if action == "retime" and not TIME.match(cue_time):
            return f'Error: retime needs cue_time as 24-hour HH:MM, got "{cue_time}".'
        turn.changes = [c for c in turn.changes if c.habit_id != habit_id]
        turn.changes.append(
            Change(habit_id, action, cue_time if action == "retime" else None, reason.strip()[:REASON_LENGTH])
        )
        return "Proposal card shown to the parent. It is not applied until they approve it with their PIN."

    return [get_progress, research, recommend_habits, propose_plan_change]
