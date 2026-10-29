from typing import Annotated

from pydantic import Field, field_validator, model_validator

from habitquest_backend.schemas import Anchor, LevelName, StrictModel, World, unique

Slug = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]*$", max_length=60)]
Strength = Annotated[float, Field(gt=0, le=1)]
Title = Annotated[str, Field(min_length=1, max_length=120)]
Instruction = Annotated[str, Field(min_length=1, max_length=300)]


class LevelDraft(StrictModel):
    level: LevelName
    minutes: int = Field(ge=1, le=60)
    points: int = Field(ge=1, le=3)
    instruction: Instruction | None = None


class VariantDraft(StrictModel):
    title: Title
    instruction: Instruction


class HabitDraft(StrictModel):
    title: Title
    instruction: Instruction
    min_age: int = Field(ge=6, le=13)
    max_age: int = Field(ge=6, le=13)
    routine_anchor: Anchor
    safety_notes: str = Field(default="", max_length=300)
    levels: list[LevelDraft] = Field(min_length=1, max_length=3)
    goals: dict[Slug, Strength] = Field(min_length=1, max_length=6)
    prepares: list[Slug] = Field(default_factory=list, max_length=5)
    tags: list[Annotated[str, Field(pattern=r"^(interest|topic|access):[a-z_]+$")]] = Field(
        default_factory=list, max_length=12
    )
    variants: dict[Annotated[str, Field(pattern=r"^[a-z_]+$")], VariantDraft] = Field(
        default_factory=dict, max_length=8
    )

    @field_validator("prepares", "tags")
    @classmethod
    def unique_values(cls, value: list) -> list:
        return unique(value)

    @field_validator("levels")
    @classmethod
    def unique_levels(cls, value: list[LevelDraft]) -> list[LevelDraft]:
        unique([level.level for level in value])
        return value

    @model_validator(mode="after")
    def age_range(self) -> "HabitDraft":
        if self.min_age > self.max_age:
            raise ValueError("min_age must not exceed max_age")
        return self


class GoalDraft(StrictModel):
    title: Title
    description: str = Field(default="", max_length=500)


class ConcernDraft(StrictModel):
    label: Title
    synonyms: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(
        default_factory=list, max_length=12
    )
    goals: dict[Slug, Strength] = Field(min_length=1, max_length=6)

    @field_validator("synonyms")
    @classmethod
    def unique_synonyms(cls, value: list[str]) -> list[str]:
        return unique(value)


class ChapterDraft(StrictModel):
    world: World
    goal_id: Slug
    title: Title


class Reason(StrictModel):
    reason: str = Field(min_length=1, max_length=300)


class ReleaseBuild(StrictModel):
    notes: str = Field(default="", max_length=500)
    source_release_id: int | None = Field(default=None, ge=1)
