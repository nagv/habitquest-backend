"""Deterministic ranking: an LLM hint, the tie-breaker and the reviewed template fallback."""

from dataclasses import dataclass

from habitquest_backend.catalog.eligibility import Candidate

RANKING_VERSION = "2026-10-29.1"
INTEREST_BONUS = 0.5
PREREQ_BONUS = 0.3
LEVEL_PENALTY = {"simple": 0.0, "standard": 0.1, "stretch": 0.3}
# A habit fully satisfies a goal it serves at least this strongly; weaker links are secondary.
PRIMARY_STRENGTH = 0.8


@dataclass(frozen=True)
class Scored:
    candidate: Candidate
    score: float
    breakdown: dict[str, float]


@dataclass(frozen=True)
class TemplatePlan:
    active: tuple[Scored, ...]
    future: tuple[Scored, ...]
    unresolved: dict[str, str]


def score(candidates: list[Candidate], goal_ids: list[str], interests: frozenset[str]) -> list[Scored]:
    """Rank candidates; goal_ids is in parent priority order (first is most important)."""
    goal_ids = list(dict.fromkeys(goal_ids))
    weights = {goal: (len(goal_ids) - i) / len(goal_ids) for i, goal in enumerate(goal_ids)}
    present = {c.habit.id for c in candidates}
    result = []
    for c in candidates:
        breakdown = {
            "goal": round(sum(weights.get(g, 0.0) * c.habit.goals[g] for g in c.goal_ids), 4),
            "interest": INTEREST_BONUS if any(f"interest:{i}" in c.habit.tags for i in interests) else 0.0,
            "prereq": PREREQ_BONUS if present & set(c.habit.supports) else 0.0,
            "level": -LEVEL_PENALTY[c.level.level],
        }
        result.append(Scored(c, round(sum(breakdown.values()), 4), breakdown))
    return sorted(result, key=lambda s: (-s.score, s.candidate.habit.id))


def shortlists(scored: list[Scored], goal_ids: list[str], size: int | None = 3) -> dict[str, list[Scored]]:
    return {goal: [s for s in scored if goal in s.candidate.goal_ids][:size] for goal in goal_ids}


def template_plan(
    lists: dict[str, list[Scored]], goal_ids: list[str], *, max_active: int, available_minutes: int
) -> TemplatePlan:
    """Top fitting habit per goal within the limits; one habit counts once and weak links never block a
    dedicated habit."""
    active: list[Scored] = []
    covered: set[str] = set()
    unresolved: dict[str, str] = {}
    minutes = 0
    for goal in goal_ids:
        if goal in covered:
            continue
        chosen = {s.candidate.habit.id for s in active}
        pending = [s for s in lists.get(goal, []) if s.candidate.habit.id not in chosen]
        options = [s for s in pending if not s.candidate.needs_cue]
        if not lists.get(goal):
            unresolved[goal] = "no_catalog_entry"
        elif not pending:
            covered.add(goal)  # only weakly served by an already active habit
        elif not options:
            unresolved[goal] = "needs_cue"
        else:
            pick = next(
                (s for s in options if minutes + s.candidate.level.minutes <= available_minutes), None
            )
            if pick is None or len(active) >= max_active:
                unresolved[goal] = "deferred"
                continue
            active.append(pick)
            minutes += pick.candidate.level.minutes
            served = {g for g in pick.candidate.goal_ids if pick.candidate.habit.goals[g] >= PRIMARY_STRENGTH}
            covered.update(served | {goal})
            for resolved in served:
                unresolved.pop(resolved, None)
    seen = {s.candidate.habit.id for s in active}
    future = []
    for goal in goal_ids:
        for s in lists.get(goal, []):
            if s.candidate.habit.id not in seen:
                seen.add(s.candidate.habit.id)
                future.append(s)
    future.sort(key=lambda s: (-s.score, s.candidate.habit.id))
    return TemplatePlan(tuple(active), tuple(future), unresolved)
