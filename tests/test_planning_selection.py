import pytest
from catalog_factories import make_habit

from habitquest_backend.catalog.eligibility import Candidate
from habitquest_backend.catalog.models import Level
from habitquest_backend.catalog.scoring import score, shortlists, template_plan
from habitquest_backend.planning import prioritise, validate_picks
from habitquest_backend.schemas import MappedGoal, Pick, PlannerSelection

A = make_habit("a", goals={"x": 1.0}, levels=(Level("simple", 3, 1), Level("standard", 6, 2)))
B = make_habit("b", goals={"y": 1.0})
C = make_habit("c", goals={"x": 1.0})
CUE = make_habit("cue", goals={"y": 1.0})
GOALS = ["x", "y"]


def options():
    candidates = [
        Candidate(A, ("x",), A.levels[0], False),
        Candidate(B, ("y",), B.levels[0], False),
        Candidate(C, ("x",), C.levels[0], False),
        Candidate(CUE, ("y",), CUE.levels[0], True),
    ]
    ranked = score(candidates, GOALS, frozenset())
    lists = shortlists(ranked, GOALS, size=None)
    template = template_plan(lists, GOALS, max_active=3, available_minutes=20)
    return shortlists(ranked, GOALS), template, lists


def selection(*picks):
    return PlannerSelection(
        picks=[Pick(habit_id=h, level=level) for h, level in picks],
        rationale="Because of the parent's goals",
        order_explanation="Follows the goal order",
    )


def test_alternative_valid_picks_are_accepted():
    short, template, lists = options()
    chosen = validate_picks(
        selection(("c", "standard"), ("b", "standard")),
        short,
        template,
        lists=lists,
        max_active=3,
        available_minutes=20,
    )
    assert [(s.candidate.habit.id, level.level) for s, level in chosen] == [
        ("c", "standard"),
        ("b", "standard"),
    ]


@pytest.mark.parametrize(
    "picks,max_active,minutes,message",
    [
        ((("a", "simple"), ("a", "simple")), 3, 20, "shortlist"),
        ((("zzz", "standard"),), 3, 20, "shortlist"),
        ((("a", "stretch"), ("b", "standard")), 3, 20, "agreed cue"),
        ((("a", "simple"), ("cue", "standard")), 3, 20, "agreed cue"),
        ((("a", "simple"), ("b", "standard"), ("c", "standard")), 2, 20, "limits"),
        ((("a", "standard"), ("b", "standard")), 3, 10, "limits"),
        ((("a", "simple"),), 3, 20, "limits"),
    ],
)
def test_invalid_picks_are_rejected(picks, max_active, minutes, message):
    short, template, lists = options()
    with pytest.raises(ValueError, match=message):
        validate_picks(
            selection(*picks), short, template, lists=lists, max_active=max_active, available_minutes=minutes
        )


def test_weak_link_pick_that_drops_a_dedicated_habit_is_rejected():
    plate = make_habit("plate", goals={"resp": 1.0, "meals": 0.5}, levels=(Level("standard", 2, 1),))
    meal = make_habit("meal", goals={"meals": 1.0})
    goals = ["resp", "meals"]
    ranked = score(
        [
            Candidate(plate, ("resp", "meals"), plate.levels[0], False),
            Candidate(meal, ("meals",), meal.levels[0], False),
        ],
        goals,
        frozenset(),
    )
    lists = shortlists(ranked, goals, size=None)
    template = template_plan(lists, goals, max_active=3, available_minutes=20)
    assert [s.candidate.habit.id for s in template.active] == ["plate", "meal"]
    short = shortlists(ranked, goals)
    kwargs = {"lists": lists, "max_active": 3, "available_minutes": 20}
    assert (
        len(validate_picks(selection(("plate", "standard"), ("meal", "standard")), short, template, **kwargs))
        == 2
    )
    with pytest.raises(ValueError, match="drop a goal"):
        validate_picks(selection(("plate", "standard")), short, template, **kwargs)


def test_prioritise_by_confidence_then_position():
    goals = [
        MappedGoal(goal_id="late", evidence_span="plates", confidence=0.9),
        MappedGoal(goal_id="early", evidence_span="bed", confidence=0.9),
        MappedGoal(goal_id="weak", evidence_span="bed", confidence=0.4),
        MappedGoal(goal_id="absent", evidence_span="nowhere", confidence=0.9),
    ]
    ordered = prioritise(goals, "At bed time, plates are everywhere")
    assert [g.goal_id for g in ordered] == ["early", "late", "absent", "weak"]
