from dataclasses import dataclass, field
from typing import Literal

Anchor = Literal["morning", "after_school", "evening", "bedtime"]
LevelName = Literal["simple", "standard", "stretch"]
AgeBand = Literal["6-9", "10-13"]
LEVEL_ORDER: tuple[LevelName, ...] = ("simple", "standard", "stretch")


@dataclass(frozen=True)
class Level:
    level: LevelName
    minutes: int
    points: int
    instruction: str | None = None


@dataclass(frozen=True)
class Habit:
    id: str
    version: int
    title: str
    instruction: str
    min_age: int
    max_age: int
    routine_anchor: Anchor
    levels: tuple[Level, ...]
    goals: dict[str, float]
    tags: frozenset[str]
    variants: dict[str, tuple[str, str]]
    supports: dict[str, str]
    withdrawn: bool = False

    def presented(self, interests: frozenset[str]) -> tuple[str, str]:
        """Reviewed child-facing title and instruction; interest variants never change the activity."""
        for interest in sorted(interests):
            if interest in self.variants:
                return self.variants[interest]
        return self.title, self.instruction


@dataclass(frozen=True)
class Goal:
    id: str
    version: int
    title: str
    description: str


@dataclass(frozen=True)
class Concern:
    id: str
    version: int
    label: str
    synonyms: tuple[str, ...]
    goals: dict[str, float]


@dataclass(frozen=True)
class Chapter:
    id: str
    version: int
    world: str
    goal_id: str
    title: str


@dataclass(frozen=True)
class Release:
    id: int
    goals: dict[str, Goal]
    concerns: dict[str, Concern]
    habits: dict[str, Habit]
    chapters: dict[tuple[str, str], Chapter]


@dataclass(frozen=True)
class RoutineSlot:
    cue: str
    days: tuple[str, ...]


@dataclass(frozen=True)
class ChildProfile:
    age_band: AgeBand
    interests: frozenset[str] = frozenset()
    accessibility: frozenset[str] = frozenset()
    opted_in_topics: frozenset[str] = frozenset()
    routine_schedule: dict[str, RoutineSlot] = field(default_factory=dict)


def profile_from_row(row: dict) -> ChildProfile:
    """Build the eligibility profile from an `hq.children` row."""
    return ChildProfile(
        age_band=row["age_band"],
        interests=frozenset(row["interests"]),
        accessibility=frozenset(row["accessibility"]),
        opted_in_topics=frozenset(row["opted_in_topics"]),
        routine_schedule={
            anchor: RoutineSlot(slot["cue"], tuple(slot["days"]))
            for anchor, slot in row["routine_schedule"].items()
        },
    )
