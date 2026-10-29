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


def planner_shortlists(
    scored: list[Scored], goal_ids: list[str], template: TemplatePlan, size: int = 3
) -> dict[str, list[Scored]]:
    """Per goal, the top cue-ready options plus any template pick for that goal, so the Planner can
    always reproduce the reviewed baseline."""
    result = {}
    for goal in goal_ids:
        options = [s for s in scored if goal in s.candidate.goal_ids and not s.candidate.needs_cue][:size]
        options += [s for s in template.active if goal in s.candidate.goal_ids and s not in options]
        result[goal] = options
    return result


def habit_covers(scored: Scored, chosen_ids: set[str], lists: dict[str, list[Scored]]) -> set[str]:
    """Goals a chosen habit covers: served strongly, or served at all once every cue-ready option for the
    goal is chosen (no unpicked dedicated alternative remains)."""
    return {
        goal
        for goal in scored.candidate.goal_ids
        if scored.candidate.habit.goals[goal] >= PRIMARY_STRENGTH
        or all(s.candidate.habit.id in chosen_ids for s in lists.get(goal, []) if not s.candidate.needs_cue)
    }


def covered_goals(chosen: list[Scored], goal_ids: list[str], lists: dict[str, list[Scored]]) -> set[str]:
    ids = {s.candidate.habit.id for s in chosen}
    return {goal for s in chosen for goal in habit_covers(s, ids, lists) if goal in goal_ids}


def template_plan(
    lists: dict[str, list[Scored]], goal_ids: list[str], *, max_active: int, available_minutes: int
) -> TemplatePlan:
    """Top fitting habit per goal within the limits, preferring one that serves the goal strongly; one habit
    counts once and weak links never block a dedicated habit. A goal left only weakly served while a
    dedicated option remains unpicked is deferred, so the template always passes strict coverage."""
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
            fitting = [s for s in options if minutes + s.candidate.level.minutes <= available_minutes]
            pick = next(
                (s for s in fitting if s.candidate.habit.goals[goal] >= PRIMARY_STRENGTH),
                fitting[0] if fitting else None,
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
    strict = covered_goals(active, goal_ids, lists)
    for goal in goal_ids:
        if goal not in strict:
            unresolved.setdefault(goal, "deferred")
    seen = {s.candidate.habit.id for s in active}
    future = []
    for goal in goal_ids:
        for s in lists.get(goal, []):
            if s.candidate.habit.id not in seen:
                seen.add(s.candidate.habit.id)
                future.append(s)
    future.sort(key=lambda s: (-s.score, s.candidate.habit.id))
    return TemplatePlan(tuple(active), tuple(future), unresolved)
