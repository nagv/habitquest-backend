import pytest
from catalog_factories import make_habit

from habitquest_backend.catalog.eligibility import Candidate
from habitquest_backend.catalog.models import Level
from habitquest_backend.catalog.scoring import score, shortlists, template_plan
from habitquest_backend.planning import validate_picks
from habitquest_backend.schemas import PausedGoal, Pick, PlannerSelection

A = make_habit("a", goals={"x": 1.0}, levels=(Level("standard", 4, 1),))
B = make_habit("b", goals={"y": 1.0}, levels=(Level("standard", 4, 1),))
C = make_habit("c", goals={"x": 1.0}, levels=(Level("standard", 4, 1),))
GOALS = ["x", "y"]
CARRIED = [
    {
        "habit_id": "a",
        "version": 1,
        "goal_ids": ["x"],
        "covers": ["x"],
        "level": "standard",
        "minutes": 4,
    }
]


def make_short():
    cands = [
        Candidate(A, ("x",), A.levels[0], False),
        Candidate(B, ("y",), B.levels[0], False),
        Candidate(C, ("x",), C.levels[0], False),
    ]
    ranked = score(cands, GOALS, frozenset())
    return (
        shortlists(ranked, GOALS),
        template_plan(shortlists(ranked, GOALS, size=None), GOALS, max_active=3, available_minutes=20),
        shortlists(ranked, GOALS, size=None),
    )


def sel(*picks, paused_goals=None):
    result_picks = []
    for h, lv, op, r in picks:
        kwargs = {"habit_id": h, "level": lv, "op": op}
        if r is not None:
            kwargs["replaces"] = r
        result_picks.append(Pick(**kwargs))
    return PlannerSelection(
        picks=result_picks,
        paused_goals=paused_goals or [],
        rationale="test rationale",
        order_explanation="test order",
    )


def test_replace_swaps_carried_habit():
    short, template, lists = make_short()
    chosen = validate_picks(
        sel(("c", "standard", "replace", "a"), ("b", "standard", "add", None)),
        short,
        template,
        lists=lists,
        max_active=3,
        available_minutes=20,
        carried=CARRIED,
    )
    assert [s.candidate.habit.id for s, _ in chosen] == ["c", "b"]


def test_rotate_to_future_displaces_carried():
    short, template, lists = make_short()
    chosen = validate_picks(
        sel(("c", "standard", "rotate_to_future", "a"), ("b", "standard", "add", None)),
        short,
        template,
        lists=lists,
        max_active=3,
        available_minutes=20,
        carried=CARRIED,
    )
    assert [s.candidate.habit.id for s, _ in chosen] == ["c", "b"]


def test_replace_invalid_replaces_is_rejected():
    short, template, lists = make_short()
    with pytest.raises(ValueError, match="replaces"):
        validate_picks(
            sel(("c", "standard", "replace", "unknown"), ("b", "standard", "add", None)),
            short,
            template,
            lists=lists,
            max_active=3,
            available_minutes=20,
            carried=CARRIED,
        )


def test_pause_goal_removes_coverage_requirement():
    short, template, lists = make_short()
    chosen = validate_picks(
        PlannerSelection(
            picks=[Pick(habit_id="b", level="standard")],
            paused_goals=[PausedGoal(goal_id="x", reason="not ready")],
            rationale="test rationale",
            order_explanation="test order",
        ),
        short,
        template,
        lists=lists,
        max_active=3,
        available_minutes=20,
        carried=[],
    )
    assert [s.candidate.habit.id for s, _ in chosen] == ["b"]


def test_pause_goal_for_unknown_goal_is_rejected():
    short, template, lists = make_short()
    with pytest.raises(ValueError, match="paused"):
        validate_picks(
            PlannerSelection(
                picks=[Pick(habit_id="b", level="standard")],
                paused_goals=[PausedGoal(goal_id="invented", reason="x")],
                rationale="test rationale",
                order_explanation="test order",
            ),
            short,
            template,
            lists=lists,
            max_active=3,
            available_minutes=20,
            carried=[],
        )
