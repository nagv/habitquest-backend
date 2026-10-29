from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

Category = Literal["bedtime", "morning", "meals", "responsibility"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ChildInput(StrictModel):
    name: str = Field(min_length=1, max_length=40)
    age_band: Literal["6-9", "10-13"]


class OnboardingInput(StrictModel):
    concerns: str = Field(min_length=5, max_length=4000)
    world: Literal["space", "forest", "city"] = "space"
    available_minutes: int = Field(default=10, ge=1, le=60)
    bedtime: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    morning: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    goals: list[Category] = Field(default_factory=list, max_length=4)
    timezone: str = "UTC"

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Use a valid IANA household timezone") from exc
        return value

    @field_validator("goals")
    @classmethod
    def unique_goals(cls, value: list[Category]) -> list[Category]:
        if len(value) != len(set(value)):
            raise ValueError("Goals must be unique")
        return value


class GoalConfirmation(StrictModel):
    expected_version: int = Field(ge=1)
    goals: list[Category] = Field(min_length=1, max_length=4)


class Confirmation(StrictModel):
    expected_plan_version: int = Field(ge=0)


class Selection(StrictModel):
    option_ids: list[str] = Field(min_length=1, max_length=4)
    rationale: str = Field(min_length=5, max_length=500)


class Review(StrictModel):
    accepted: bool
    reason: str = Field(max_length=200)


class GoalAnalysis(StrictModel):
    goals: list[Category] = Field(max_length=4)
    explanation: str = Field(min_length=5, max_length=500)


class PreferenceInput(StrictModel):
    kind: Literal["routine_times", "world"]
    value: dict[str, str] | str
    source_id: UUID


LevelName = Literal["simple", "standard", "stretch"]


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
