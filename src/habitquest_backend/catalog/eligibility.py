from dataclasses import dataclass

from habitquest_backend.catalog.models import LEVEL_ORDER, ChildProfile, Habit, Level, Release

AGE_BANDS = {"6-9": (6, 9), "10-13": (10, 13)}


@dataclass(frozen=True)
class Candidate:
    habit: Habit
    goal_ids: tuple[str, ...]
    level: Level
    needs_cue: bool


def eligible(
    release: Release,
    profile: ChildProfile,
    goal_ids: list[str],
    *,
    available_minutes: int,
    active_habit_ids: frozenset[str] = frozenset(),
) -> list[Candidate]:
    """Deterministic hard filter; the LLM and scoring only ever see these candidates."""
    low, high = AGE_BANDS[profile.age_band]
    required_access = {f"access:{need}" for need in profile.accessibility}
    result = []
    for habit in sorted(release.habits.values(), key=lambda h: h.id):
        served = tuple(goal for goal in goal_ids if goal in habit.goals)
        topics = {tag.removeprefix("topic:") for tag in habit.tags if tag.startswith("topic:")}
        if (
            habit.withdrawn
            or not served
            or habit.id in active_habit_ids
            or not (habit.min_age <= low and habit.max_age >= high)
            or not topics <= profile.opted_in_topics
            or not required_access <= habit.tags
        ):
            continue
        levels = sorted(habit.levels, key=lambda level: LEVEL_ORDER.index(level.level))
        level = next((lv for lv in levels if lv.minutes <= available_minutes), None)
        if level is None:
            continue
        result.append(Candidate(habit, served, level, habit.routine_anchor not in profile.routine_schedule))
    return result
