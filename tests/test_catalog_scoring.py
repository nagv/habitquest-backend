import pytest
from catalog_factories import make_habit, make_release
from hypothesis import given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st

from habitquest_backend.catalog.eligibility import Candidate, eligible
from habitquest_backend.catalog.models import ChildProfile, Level, RoutineSlot
from habitquest_backend.catalog.scoring import (
    PRIMARY_STRENGTH,
    covered_goals,
    planner_shortlists,
    score,
    shortlists,
    template_plan,
)

PROFILE = ChildProfile(
    age_band="6-9",
    interests=frozenset({"space"}),
    routine_schedule={"bedtime": RoutineSlot("20:00", ("mon",)), "evening": RoutineSlot("19:00", ("mon",))},
)


def candidate(habit, goal_ids=("g",), level=None, needs_cue=False):
    return Candidate(habit, tuple(goal_ids), level or habit.levels[0], needs_cue)


def test_score_combines_priority_interest_prerequisite_and_level():
    bag = make_habit("bag", goals={"r": 0.8}, tags={"interest:space"}, supports={"morning": "prepares"})
    morning = make_habit("morning", goals={"m": 1.0}, levels=(Level("simple", 3, 1),))
    # Weights for ["m", "r"]: m=1.0, r=0.5. Bag: 0.5*0.8 + 0.5 + 0.3 - 0.1 = 1.1; morning: 1.0.
    [first, second] = score(
        [candidate(bag, ("r",)), candidate(morning, ("m",))], ["m", "r"], PROFILE.interests
    )
    assert first.candidate.habit.id == "bag"
    assert first.breakdown == {"goal": 0.4, "interest": 0.5, "prereq": 0.3, "level": -0.1}
    assert first.score == 1.1
    assert second.breakdown == {"goal": 1.0, "interest": 0.0, "prereq": 0.0, "level": 0.0}
    assert second.score == 1.0


def test_ties_break_by_habit_id():
    result = score([candidate(make_habit("b")), candidate(make_habit("a"))], ["g"], frozenset())
    assert [s.candidate.habit.id for s in result] == ["a", "b"]


def test_shortlists_are_per_goal_and_capped():
    scored = score([candidate(make_habit(str(i))) for i in range(5)], ["g", "empty"], frozenset())
    lists = shortlists(scored, ["g", "empty"], size=3)
    assert [s.candidate.habit.id for s in lists["g"]] == ["0", "1", "2"]
    assert lists["empty"] == []
    assert len(shortlists(scored, ["g"], size=None)["g"]) == 5


def test_template_plan_counts_multi_goal_habit_once_and_reports_reasons():
    plate = make_habit("plate", goals={"meals": 0.5, "resp": 1.0}, levels=(Level("standard", 2, 1),))
    morning = make_habit("morning", goals={"morning": 1.0}, anchor="morning")
    bag = make_habit("bag", goals={"resp": 0.8}, anchor="evening")
    release = make_release(plate, morning, bag)
    goals = ["resp", "meals", "morning", "bedtime"]
    candidates = eligible(release, PROFILE, goals, available_minutes=10)
    plan = template_plan(
        shortlists(score(candidates, goals, PROFILE.interests), goals),
        goals,
        max_active=3,
        available_minutes=10,
    )
    assert [s.candidate.habit.id for s in plan.active] == ["plate"]
    assert [s.candidate.habit.id for s in plan.future] == ["bag", "morning"]
    assert plan.unresolved == {"morning": "needs_cue", "bedtime": "no_catalog_entry"}


def test_template_plan_defers_goals_beyond_active_or_minute_limits():
    a = make_habit("a", goals={"x": 1.0}, levels=(Level("standard", 6, 1),))
    b = make_habit("b", goals={"y": 1.0}, levels=(Level("standard", 6, 1),))
    c = make_habit("c", goals={"z": 1.0}, levels=(Level("standard", 1, 1),))
    goals = ["x", "y", "z"]
    lists = shortlists(
        score([candidate(a, ("x",)), candidate(b, ("y",)), candidate(c, ("z",))], goals, frozenset()), goals
    )
    by_minutes = template_plan(lists, goals, max_active=3, available_minutes=10)
    assert [s.candidate.habit.id for s in by_minutes.active] == ["a", "c"]
    assert by_minutes.unresolved == {"y": "deferred"}
    by_count = template_plan(lists, goals, max_active=1, available_minutes=60)
    assert by_count.unresolved == {"y": "deferred", "z": "deferred"}


@pytest.mark.parametrize("strength,unresolved", [(0.9, {}), (0.2, {"x": "deferred"})])
def test_only_a_strong_secondary_link_resolves_an_earlier_deferred_goal(strength, unresolved):
    big = make_habit("big", goals={"x": 1.0}, levels=(Level("standard", 9, 1),))
    both = make_habit("both", goals={"x": strength, "y": 1.0}, levels=(Level("standard", 3, 1),))
    goals = ["x", "y"]
    lists = {
        "x": score([candidate(big, ("x",))], goals, frozenset()),
        "y": score([candidate(both, ("x", "y"))], goals, frozenset()),
    }
    plan = template_plan(lists, goals, max_active=3, available_minutes=5)
    assert [s.candidate.habit.id for s in plan.active] == ["both"]
    assert plan.unresolved == unresolved


def test_weak_link_does_not_block_a_dedicated_habit():
    plate = make_habit("plate", goals={"resp": 1.0, "meals": 0.5}, levels=(Level("standard", 2, 1),))
    meal = make_habit("meal", goals={"meals": 1.0}, levels=(Level("standard", 5, 1),))
    goals = ["resp", "meals"]
    cands = [candidate(plate, ("resp", "meals")), candidate(meal, ("meals",))]
    plan = template_plan(
        shortlists(score(cands, goals, frozenset()), goals), goals, max_active=3, available_minutes=10
    )
    assert [s.candidate.habit.id for s in plan.active] == ["plate", "meal"]


def test_template_prefers_a_dedicated_habit_over_a_weak_link_for_its_goal():
    # Both score 1.0 for ["meals", "resp"]; the tie would otherwise pick the weak "clear" for meals.
    clear = make_habit("clear", goals={"meals": 0.5, "resp": 1.0}, levels=(Level("standard", 2, 1),))
    meal = make_habit("meal", goals={"meals": 1.0}, levels=(Level("standard", 5, 1),))
    goals = ["meals", "resp"]
    lists = shortlists(
        score([candidate(clear, ("meals", "resp")), candidate(meal, ("meals",))], goals, frozenset()),
        goals,
        size=None,
    )
    plan = template_plan(lists, goals, max_active=3, available_minutes=10)
    assert [s.candidate.habit.id for s in plan.active] == ["meal", "clear"]
    assert plan.unresolved == {}
    assert covered_goals(list(plan.active), goals, lists) == {"meals", "resp"}


def test_template_defers_a_goal_only_weakly_served_while_a_dedicated_habit_remains():
    clear = make_habit("clear", goals={"meals": 0.5}, levels=(Level("standard", 2, 1),))
    meal = make_habit("meal", goals={"meals": 1.0}, levels=(Level("standard", 9, 1),))
    goals = ["meals"]
    lists = shortlists(
        score([candidate(clear, ("meals",)), candidate(meal, ("meals",))], goals, frozenset()),
        goals,
        size=None,
    )
    plan = template_plan(lists, goals, max_active=3, available_minutes=5)
    assert [s.candidate.habit.id for s in plan.active] == ["clear"]
    assert plan.unresolved == {"meals": "deferred"}


def coverage_lists(*extra):
    plate = make_habit("plate", goals={"resp": 1.0, "meals": 0.5})
    habits = [candidate(plate, ("resp", "meals")), *extra]
    ranked = score(habits, ["resp", "meals"], frozenset())
    return ranked, shortlists(ranked, ["resp", "meals"], size=None)


def test_covered_goals_strong_link():
    ranked, lists = coverage_lists()
    assert covered_goals(ranked, ["resp"], lists) == {"resp"}


def test_covered_goals_weak_link_with_a_dedicated_alternative_is_not_covered():
    meal = make_habit("meal", goals={"meals": 1.0})
    ranked, lists = coverage_lists(candidate(meal, ("meals",)))
    plate = [s for s in ranked if s.candidate.habit.id == "plate"]
    assert covered_goals(plate, ["resp", "meals"], lists) == {"resp"}
    assert covered_goals(ranked, ["resp", "meals"], lists) == {"resp", "meals"}


def test_covered_goals_weak_link_without_a_usable_alternative_is_covered():
    cue = make_habit("cue", goals={"meals": 1.0})
    ranked, lists = coverage_lists(candidate(cue, ("meals",), needs_cue=True))
    plate = [s for s in ranked if s.candidate.habit.id == "plate"]
    assert covered_goals(plate, ["resp", "meals"], lists) == {"resp", "meals"}
    assert covered_goals([], ["resp", "meals"], lists) == set()


def test_planner_shortlists_skip_cue_options_and_include_template_picks():
    cues = [candidate(make_habit(f"c{i}"), needs_cue=True) for i in range(3)]
    ready = [candidate(make_habit(f"r{i}", levels=(Level("stretch", 5, 1),))) for i in range(4)]
    ranked = score(cues + ready, ["g"], frozenset())
    assert [s.candidate.habit.id for s in ranked][:3] == ["c0", "c1", "c2"]
    pick = next(s for s in ranked if s.candidate.habit.id == "r3")
    template = template_plan({"g": [pick]}, ["g"], max_active=3, available_minutes=10)
    short = planner_shortlists(ranked, ["g", "none"], template)
    assert [s.candidate.habit.id for s in short["g"]] == ["r0", "r1", "r2", "r3"]
    assert short["none"] == []
    assert len(planner_shortlists(ranked, ["g"], template, size=5)["g"]) == 4


GOALS = ["a", "b", "c", "d"]
ANCHORS = ["morning", "after_school", "evening", "bedtime"]
habits = st.lists(
    st.builds(
        lambda i, goals, minutes, anchor: make_habit(
            f"h{i}", goals=goals, anchor=anchor, levels=(Level("standard", minutes, 1),)
        ),
        st.integers(0, 30),
        st.dictionaries(st.sampled_from(GOALS), st.sampled_from([0.5, 0.8, 1.0]), min_size=1),
        st.integers(1, 20),
        st.sampled_from(ANCHORS),
    ),
    unique_by=lambda h: h.id,
    max_size=12,
)


@hypothesis_settings(max_examples=200, deadline=None)
@given(
    habits,
    st.lists(st.sampled_from(GOALS), unique=True, min_size=1),
    st.integers(1, 3),
    st.integers(1, 60),
)
def test_template_plan_invariants(habit_list, goals, max_active, minutes):
    candidates = eligible(make_release(*habit_list), PROFILE, goals, available_minutes=minutes)
    lists = shortlists(score(candidates, goals, PROFILE.interests), goals, size=None)
    plan = template_plan(lists, goals, max_active=max_active, available_minutes=minutes)
    active_ids = [s.candidate.habit.id for s in plan.active]
    assert len(active_ids) == len(set(active_ids)) <= max_active
    assert sum(s.candidate.level.minutes for s in plan.active) <= minutes
    assert not any(s.candidate.needs_cue for s in plan.active)
    served = {g for s in plan.active for g in s.candidate.goal_ids}
    primary = {
        g for s in plan.active for g in s.candidate.goal_ids if s.candidate.habit.goals[g] >= PRIMARY_STRENGTH
    }
    assert set(goals) <= served | set(plan.unresolved)
    assert primary.isdisjoint(plan.unresolved)
    assert not set(active_ids) & {s.candidate.habit.id for s in plan.future}
    assert set(goals) - set(plan.unresolved) <= covered_goals(list(plan.active), goals, lists)
    assert plan == template_plan(lists, goals, max_active=max_active, available_minutes=minutes)
