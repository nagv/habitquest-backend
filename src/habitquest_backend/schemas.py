from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

World = Literal["space", "forest", "city"]
Interest = Literal["space", "animals", "art", "sports", "music", "building", "nature", "reading"]
Access = Literal["short_instructions", "visual_cues", "extra_time"]
Topic = Literal["puberty_hygiene"]
Anchor = Literal["morning", "after_school", "evening", "bedtime"]
Day = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
LevelName = Literal["simple", "standard", "stretch"]


def unique(value: list) -> list:
    if len(value) != len(set(value)):
        raise ValueError("Values must be unique")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RoutineSlotInput(StrictModel):
    cue: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    days: list[Day] = Field(min_length=1, max_length=7)

    @field_validator("days")
    @classmethod
    def unique_days(cls, value: list[Day]) -> list[Day]:
        return unique(value)


class ChildInput(StrictModel):
    name: str = Field(min_length=1, max_length=40)
    age_band: Literal["6-9", "10-13"]
    interests: list[Interest] = Field(default_factory=list, max_length=8)
    accessibility: list[Access] = Field(default_factory=list, max_length=3)
    opted_in_topics: list[Topic] = Field(default_factory=list, max_length=1)
    preferred_world: World = "space"
    routine_schedule: dict[Anchor, RoutineSlotInput] = Field(default_factory=dict)
    timezone: str = "UTC"

    @field_validator("interests", "accessibility", "opted_in_topics")
    @classmethod
    def unique_values(cls, value: list) -> list:
        return unique(value)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Use a valid IANA household timezone") from exc
        return value


class ProfileUpdate(ChildInput):
    expected_profile_version: int = Field(ge=1)


class IntakeInput(StrictModel):
    concerns: str = Field(min_length=5, max_length=4000)
    available_minutes: int = Field(default=10, ge=1, le=60)


class Confirmation(StrictModel):
    expected_plan_version: int = Field(ge=0)
    expected_proposal_version: int = Field(ge=1)


class MappedGoal(StrictModel):
    goal_id: str = Field(min_length=1, max_length=60)
    evidence_span: str = Field(max_length=300)
    confidence: float = Field(ge=0, le=1)
    suggested_not_stated: bool = False


class GoalMapping(StrictModel):
    goals: list[MappedGoal] = Field(max_length=8)
    unmatched_phrases: list[Annotated[str, Field(max_length=200)]] = Field(
        default_factory=list, max_length=10
    )
    explanation: str = Field(min_length=5, max_length=500)


class Pick(StrictModel):
    habit_id: str = Field(min_length=1, max_length=60)
    level: LevelName


class PlannerSelection(StrictModel):
    picks: list[Pick] = Field(max_length=6)
    rationale: str = Field(min_length=5, max_length=500)
    order_explanation: str = Field(min_length=5, max_length=500)


class Review(StrictModel):
    accepted: bool
    reason: str = Field(max_length=200)
