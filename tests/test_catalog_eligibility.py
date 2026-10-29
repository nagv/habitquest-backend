from catalog_factories import make_habit, make_release

from habitquest_backend.catalog.eligibility import eligible
from habitquest_backend.catalog.models import ChildProfile, Level, RoutineSlot

SCHEDULE = {"bedtime": RoutineSlot("20:00", ("mon", "tue"))}
PROFILE = ChildProfile(age_band="6-9", routine_schedule=SCHEDULE)


def ids(candidates):
    return [c.habit.id for c in candidates]


def test_keeps_matching_habit_with_served_goals_in_priority_order():
    habit = make_habit("a", goals={"x": 1.0, "y": 0.5})
    [candidate] = eligible(make_release(habit), PROFILE, ["y", "z", "x"], available_minutes=10)
    assert candidate.goal_ids == ("y", "x")
    assert candidate.level == Level("standard", 5, 1)
    assert not candidate.needs_cue


def test_excludes_withdrawn_unrelated_active_and_age_mismatched_habits():
    release = make_release(
        make_habit("withdrawn", withdrawn=True),
        make_habit("other-goal", goals={"other": 1.0}),
        make_habit("active"),
        make_habit("too-young", min_age=8),
        make_habit("too-old", max_age=8),
        make_habit("ok"),
    )
    result = eligible(release, PROFILE, ["g"], active_habit_ids=frozenset({"active"}), available_minutes=10)
    assert ids(result) == ["ok"]


def test_topic_tags_require_parent_opt_in():
    release = make_release(make_habit("puberty", tags={"topic:puberty_hygiene"}))
    assert eligible(release, PROFILE, ["g"], available_minutes=10) == []
    opted = ChildProfile(
        age_band="6-9", opted_in_topics=frozenset({"puberty_hygiene"}), routine_schedule=SCHEDULE
    )
    assert ids(eligible(release, opted, ["g"], available_minutes=10)) == ["puberty"]


def test_every_accessibility_need_must_be_supported():
    release = make_release(
        make_habit("short", tags={"access:short_instructions"}),
        make_habit("plain"),
    )
    needs = ChildProfile(
        age_band="6-9", accessibility=frozenset({"short_instructions"}), routine_schedule=SCHEDULE
    )
    assert ids(eligible(release, needs, ["g"], available_minutes=10)) == ["short"]


def test_chooses_simplest_level_that_fits_or_excludes():
    levels = (Level("stretch", 8, 3), Level("simple", 6, 1), Level("standard", 4, 2))
    release = make_release(make_habit("a", levels=levels))
    assert eligible(release, PROFILE, ["g"], available_minutes=5)[0].level.level == "standard"
    assert eligible(release, PROFILE, ["g"], available_minutes=6)[0].level.level == "simple"
    assert eligible(release, PROFILE, ["g"], available_minutes=3) == []


def test_marks_habits_without_an_agreed_cue():
    release = make_release(make_habit("m", anchor="morning"), make_habit("b"))
    result = {c.habit.id: c.needs_cue for c in eligible(release, PROFILE, ["g"], available_minutes=10)}
    assert result == {"b": False, "m": True}


def test_10_to_13_band_uses_its_own_bounds():
    release = make_release(make_habit("young", max_age=9), make_habit("all"))
    older = ChildProfile(age_band="10-13", routine_schedule=SCHEDULE)
    assert ids(eligible(release, older, ["g"], available_minutes=10)) == ["all"]


def test_duplicate_goal_ids_are_served_once():
    [candidate] = eligible(make_release(make_habit("a")), PROFILE, ["g", "g"], available_minutes=10)
    assert candidate.goal_ids == ("g",)
