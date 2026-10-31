"""Mapping a parent's pain points to pre-approved goals, with recommended habits per goal.

Order: a local safety screen, then one structured model call (or the deterministic `fake` stand-in), whose
answer is checked against the catalog the model was shown; on any failure, keyword matching on
`goals.keywords`; when nothing matches, the parent picks goals by hand. The model never sees names from our
records, only the child's age, the parent's text and the catalog.
"""

import json
import logging
import re
from dataclasses import dataclass, field

import openai
from pydantic import BaseModel, Field

from habitquest_backend.config import Settings
from habitquest_backend.llm import chat_model

log = logging.getLogger(__name__)
MAX_GOALS = 3
MAX_RECOMMENDED = 2
# A keyword matches as a whole word or with a common ending ("tantrum" -> "tantrums"), not as any prefix
# ("late" must not match "lately").
ENDINGS = "(?:s|es|ed|ing|er|ers)?"
SAFETY_GUIDANCE = (
    "What you wrote suggests a child may be at risk of harm. If anyone is in immediate danger, call your local "
    "emergency number now. Otherwise, please talk to your child's doctor or a child mental health professional "
    "soon, or contact a local child helpline. Habit goals are not the right tool for this."
)
# Deliberately conservative: a match skips goal mapping and shows SAFETY_GUIDANCE. Not exhaustive; the model
# can also flag risk (`at_risk`).
RISK = re.compile(
    r"suicid|self[- ]?harm|(hurt|hurts|hurting|cut|cuts|cutting|kill|kills|killing)\s+(him|her|them|my)sel"
    r"|wants?\s+to\s+die|abus(e|ed|ing)|molest|sexual|touched\s+(him|her|them)\b|not\s+safe\s+at\s+home",
    re.IGNORECASE,
)
SYSTEM_PROMPT = """You help parents of children aged 3 to 13 choose habit goals in a family habit app.
Map the parent's description to goals from the given list ONLY, and answer with JSON only, in this shape:
{"at_risk": false, "goals": [{"goal_id": "...", "reason": "...", "recommended_habits": ["...", "..."]}]}
Rules:
- 1 to 3 goals, most relevant first. Use only goal ids from the list.
- reason: one short, warm sentence to the parent saying why this goal fits what they wrote.
- recommended_habits: up to 2 habit ids per goal, chosen only from that goal's habits, the best fit first.
- If the text suggests a child may be harmed, abused, in danger or talking about hurting themselves, answer
  {"at_risk": true, "goals": []}.
- Never diagnose, and never suggest punishment, shaming or taking food or sleep away."""


@dataclass
class Candidate:
    """An active goal and the habits this child could take on for it (age and unlocked tier)."""

    goal_id: str
    title: str
    description: str
    keywords: list[str]
    habits: list[dict]  # id, title, tier; easiest first


@dataclass
class Mapping:
    mapped_by: str  # ai | keywords | manual
    goals: list[tuple[str, str]] = field(default_factory=list)  # (goal_id, reason), most relevant first
    recommended: dict[str, list[str]] = field(default_factory=dict)  # goal_id -> habit ids
    safety: str | None = None


class GoalPick(BaseModel):
    goal_id: str
    reason: str = Field(default="", max_length=400)
    recommended_habits: list[str] = Field(default_factory=list, max_length=10)


class Answer(BaseModel):
    at_risk: bool = False
    goals: list[GoalPick] = Field(default_factory=list, max_length=10)


def ask_model(settings: Settings, age: int, text: str, candidates: list[Candidate]) -> dict:
    """One chat completion from the OpenAI-compatible endpoint, parsed as JSON."""
    catalog = [
        {"id": c.goal_id, "title": c.title, "description": c.description, "habits": c.habits}
        for c in candidates
    ]
    model = chat_model(settings).bind(response_format={"type": "json_object"})
    reply = model.invoke(
        [
            ("system", SYSTEM_PROMPT),
            ("user", json.dumps({"child_age": age, "parent_wrote": text, "goals": catalog})),
        ]
    )
    # Some models wrap JSON in a Markdown fence despite response_format.
    return json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", reply.text.strip()))


def keyword_goals(text: str, candidates: list[Candidate]) -> list[tuple[str, str]]:
    """Goals whose keywords appear in the text, most hits first (catalog order on ties)."""
    lowered = text.lower()
    scored = []
    for candidate in candidates:
        hits = [word for word in candidate.keywords if re.search(rf"\b{re.escape(word)}{ENDINGS}\b", lowered)]
        if hits:
            scored.append((len(hits), candidate.goal_id, "You mentioned " + ", ".join(hits[:3]) + "."))
    scored.sort(key=lambda hit: -hit[0])
    return [(goal_id, reason) for _, goal_id, reason in scored[:MAX_GOALS]]


def fake_answer(text: str, candidates: list[Candidate]) -> dict:
    """The deterministic stand-in for the model: keyword goals, and each goal's first habits."""
    habits = {c.goal_id: [h["id"] for h in c.habits] for c in candidates}
    return {
        "at_risk": False,
        "goals": [
            {"goal_id": goal_id, "reason": reason, "recommended_habits": habits[goal_id][:MAX_RECOMMENDED]}
            for goal_id, reason in keyword_goals(text, candidates)
        ],
    }


def checked(answer: Answer, candidates: list[Candidate]) -> Mapping:
    """Keeps only goals and habits from the catalog the model was shown, within the limits."""
    known = {c.goal_id: {h["id"] for h in c.habits} for c in candidates}
    mapping = Mapping("ai")
    for pick in answer.goals:
        if pick.goal_id not in known or pick.goal_id in mapping.recommended:
            continue
        habits = list(dict.fromkeys(h for h in pick.recommended_habits if h in known[pick.goal_id]))
        mapping.goals.append((pick.goal_id, pick.reason.strip()))
        mapping.recommended[pick.goal_id] = habits[:MAX_RECOMMENDED]
        if len(mapping.goals) == MAX_GOALS:
            break
    return mapping


def map_pain_point(settings: Settings, age: int, text: str, candidates: list[Candidate]) -> Mapping:
    if RISK.search(text):
        return Mapping("manual", safety=SAFETY_GUIDANCE)
    try:
        raw = (
            fake_answer(text, candidates)
            if settings.ai_mode == "fake"
            else ask_model(settings, age, text, candidates)
        )
        answer = Answer.model_validate(raw)
        if answer.at_risk:
            return Mapping("manual", safety=SAFETY_GUIDANCE)
        mapping = checked(answer, candidates)
        if mapping.goals:
            return mapping
    except (openai.OpenAIError, ValueError, KeyError, IndexError, TypeError) as exc:
        # The status code shows a provider rejecting the request (e.g. 400, 401); never log the content.
        status = exc.status_code if isinstance(exc, openai.APIStatusError) else ""
        log.warning("AI goal mapping failed (%s %s); using keyword matching", type(exc).__name__, status)
    goals = keyword_goals(text, candidates)
    return Mapping("keywords", goals) if goals else Mapping("manual")
