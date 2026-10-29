from catalog_factories import make_habit, make_release
from hypothesis import given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st

from habitquest_backend.catalog.eligibility import Candidate, eligible
from habitquest_backend.catalog.models import ChildProfile, Level, RoutineSlot
from habitquest_backend.catalog.scoring import score, shortlists, template_plan

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


def test_later_pick_resolves_an_earlier_deferred_goal():
    big = make_habit("big", goals={"x": 1.0}, levels=(Level("standard", 9, 1),))
    both = make_habit("both", goals={"x": 0.2, "y": 1.0}, levels=(Level("standard", 3, 1),))
    goals = ["x", "y"]
    lists = {
        "x": score([candidate(big, ("x",))], goals, frozenset()),
        "y": score([candidate(both, ("x", "y"))], goals, frozenset()),
    }
    plan = template_plan(lists, goals, max_active=3, available_minutes=5)
    assert [s.candidate.habit.id for s in plan.active] == ["both"]
    assert plan.unresolved == {}


GOALS = ["a", "b", "c", "d"]
ANCHORS = ["morning", "after_school", "evening", "bedtime"]
habits = st.lists(
    st.builds(
        lambda i, goals, minutes, anchor: make_habit(
            f"h{i}", goals=dict.fromkeys(goals, 1.0), anchor=anchor, levels=(Level("standard", minutes, 1),)
        ),
        st.integers(0, 30),
        st.sets(st.sampled_from(GOALS), min_size=1),
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
    lists = shortlists(score(candidates, goals, PROFILE.interests), goals)
    plan = template_plan(lists, goals, max_active=max_active, available_minutes=minutes)
    active_ids = [s.candidate.habit.id for s in plan.active]
    assert len(active_ids) == len(set(active_ids)) <= max_active
    assert sum(s.candidate.level.minutes for s in plan.active) <= minutes
    assert not any(s.candidate.needs_cue for s in plan.active)
    covered = {g for s in plan.active for g in s.candidate.goal_ids}
    assert covered.isdisjoint(plan.unresolved)
    assert covered | set(plan.unresolved) == set(goals)
    assert not set(active_ids) & {s.candidate.habit.id for s in plan.future}
    assert plan == template_plan(lists, goals, max_active=max_active, available_minutes=minutes)


def test_duplicate_and_unknown_goal_ids_do_not_distort_scores():
    habit = make_habit("a", goals={"x": 1.0, "y": 1.0})
    [dup] = score([candidate(habit, ("x",))], ["x", "x", "y"], frozenset())
    assert dup.breakdown["goal"] == 1.0
    [unknown] = score([candidate(habit, ("x", "y"))], ["x"], frozenset())
    assert unknown.breakdown["goal"] == 1.0
    assert score([], [], frozenset()) == []
