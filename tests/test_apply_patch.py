from dataclasses import replace

import pytest
from catalog_factories import make_habit
from pydantic import ValidationError

from habitquest_backend.catalog.models import Chapter, ChildProfile, Level, Release, RoutineSlot
from habitquest_backend.planning import entry
from habitquest_backend.schemas import ChangeScheduleEdit
from habitquest_backend.service import apply_patch, cue_loss_habits

LEVELS = (Level("simple", 3, 1), Level("standard", 5, 1))
HABITS = {
    "a": make_habit("a", goals={"g": 1.0}, levels=LEVELS),
    "b": make_habit("b", goals={"g": 1.0}),
    "f": make_habit(
        "f", goals={"h": 1.0}, anchor="morning", levels=(Level("simple", 3, 1), Level("standard", 6, 2))
    ),
    "e": make_habit("e", goals={"h": 1.0}, anchor="evening"),
    "w": make_habit("w", goals={"h": 1.0}, withdrawn=True),
    "x": make_habit("x", goals={"x": 1.0}),
}
CHAPTERS = {("space", g): Chapter(f"space-{g}", 1, "space", g, g.upper()) for g in ("g", "h")}
RELEASE = Release(2, {}, {}, HABITS, CHAPTERS)
PROFILE = ChildProfile(
    age_band="6-9",
    routine_schedule={"bedtime": RoutineSlot("20:00", ("mon",)), "morning": RoutineSlot("07:00", ("mon",))},
)
ACTIVE = {
    **entry(HABITS["a"], ["g"], LEVELS[1], PROFILE),
    "cue": "19:30",
    "covers": ["g"],
    "added_in_plan_version": 1,
}
DATA = {
    "world": "space",
    "goals": [{"goal_id": "g", "status": "covered"}, {"goal_id": "h", "status": "deferred"}],
    "active": [ACTIVE],
    "future": [{"habit_id": h, "version": 1, "goal_ids": ["h"], "score": 1} for h in ("f", "e", "w")],
    "shortlists": {"g": [{"habit_id": "a"}, {"habit_id": "b"}], "h": [{"habit_id": "f"}]},
    "unresolved": {"h": "deferred"},
    "chapters": [{"id": "space-g", "version": 1, "goal_id": "g", "title": "G"}],
}


def patch(data=DATA, release=RELEASE, max_active=2, minutes=12, **edit):
    return apply_patch(
        data, edit, release, PROFILE, max_active=max_active, available_minutes=minutes, plan_version=2
    )


def test_reorder_goals_requires_every_goal_once():
    assert [g["goal_id"] for g in patch(op="reorder_goals", goal_ids=["h", "g"])["goals"]] == ["h", "g"]
    for ids in (["h"], ["h", "g", "x"]):
        with pytest.raises(ValueError, match="every proposal goal"):
            patch(op="reorder_goals", goal_ids=ids)


def test_change_level_rebuilds_minutes_and_keeps_the_schedule():
    changed = patch(op="change_level", habit_id="a", level="simple")["active"][0]
    assert (changed["level"], changed["minutes"], changed["cue"], changed["covers"]) == (
        "simple",
        3,
        "19:30",
        ["g"],
    )
    with pytest.raises(ValueError, match="level: 'a' has no 'stretch' level"):
        patch(op="change_level", habit_id="a", level="stretch")
    newer = replace(RELEASE, habits={**HABITS, "a": replace(HABITS["a"], version=2)})
    with pytest.raises(ValueError, match="stale: a: version_superseded"):
        patch(release=newer, op="change_level", habit_id="a", level="simple")
    assert DATA["active"][0] == ACTIVE


def test_change_schedule():
    changed = patch(op="change_schedule", habit_id="a", cue="07:30", days=["mon"])["active"][0]
    assert (changed["cue"], changed["days"]) == ("07:30", ["mon"])


def test_swap_rebuilds_from_the_catalog_and_returns_the_old_habit_to_future():
    result = patch(op="swap_habit", old_habit_id="a", new_habit_id="b", level="standard")
    (swapped,) = result["active"]
    assert (swapped["habit_id"], swapped["title"], swapped["covers"], swapped["cue"]) == (
        "b",
        "Title b",
        ["g"],
        "20:00",
    )
    assert result["future"][-1]["habit_id"] == "a"
    with pytest.raises(ValueError, match="not in the shortlist"):
        patch(op="swap_habit", old_habit_id="a", new_habit_id="f", level="standard")
    with pytest.raises(ValueError, match="already active"):
        patch(op="swap_habit", old_habit_id="a", new_habit_id="a", level="standard")
    gone = replace(RELEASE, habits={k: v for k, v in HABITS.items() if k != "b"})
    with pytest.raises(ValueError, match="stale: b: not_in_release"):
        patch(release=gone, op="swap_habit", old_habit_id="a", new_habit_id="b", level="standard")


def test_promote_covers_the_goal_with_its_chapter_and_cue():
    result = patch(op="promote_future", habit_id="f", level="standard")
    promoted = result["active"][-1]
    assert (promoted["cue"], promoted["minutes"], promoted["covers"], promoted["added_in_plan_version"]) == (
        "07:00",
        6,
        ["h"],
        2,
    )
    assert [g["status"] for g in result["goals"]] == ["covered", "covered"]
    assert result["unresolved"] == {}
    assert [c["id"] for c in result["chapters"]] == ["space-g", "space-h"]
    assert [h["habit_id"] for h in result["future"]] == ["e", "w"]


@pytest.mark.parametrize(
    ("kwargs", "rule"),
    [
        ({"habit_id": "e", "level": "standard"}, "needs_cue: add a evening cue"),
        ({"habit_id": "w", "level": "standard"}, "stale: w: withdrawn"),
        ({"habit_id": "f", "level": "standard", "max_active": 1}, "max_active_habits"),
        (
            {"habit_id": "f", "level": "standard", "minutes": 10},
            "available_minutes: 11 minutes exceeds the daily 10",
        ),
    ],
)
def test_promote_enforces_the_plan_rules(kwargs, rule):
    with pytest.raises(ValueError, match=rule):
        patch(op="promote_future", **kwargs)


def test_promote_needs_an_approved_chapter_for_a_newly_covered_goal():
    data = {
        **DATA,
        "goals": [*DATA["goals"], {"goal_id": "x", "status": "deferred"}],
        "future": [{"habit_id": "x", "version": 1, "goal_ids": ["x"], "score": 1}],
    }
    with pytest.raises(ValueError, match="chapter: no approved space chapter for 'x'"):
        patch(data=data, op="promote_future", habit_id="x", level="standard")


def test_demote_keeps_coverage_unless_the_goal_is_removed_first():
    with pytest.raises(ValueError, match=r"coverage: \['g'\] would no longer be covered"):
        patch(op="demote_active", habit_id="a")
    removed = patch(op="remove_goal", goal_id="g")
    result = patch(data=removed, op="demote_active", habit_id="a")
    assert (result["active"], result["chapters"]) == ([], [])
    assert [h["habit_id"] for h in result["future"]] == ["f", "e", "w", "a"]


@pytest.mark.parametrize(
    "edit",
    [
        {"op": "change_level", "habit_id": "zz", "level": "simple"},
        {"op": "change_schedule", "habit_id": "zz", "cue": "07:30", "days": ["mon"]},
        {"op": "swap_habit", "old_habit_id": "zz", "new_habit_id": "b", "level": "simple"},
        {"op": "demote_active", "habit_id": "zz"},
        {"op": "promote_future", "habit_id": "zz", "level": "simple"},
        {"op": "invented"},
    ],
)
def test_unknown_targets_are_rejected(edit):
    with pytest.raises(ValueError):
        patch(**edit)


def test_change_schedule_rejects_duplicate_days():
    with pytest.raises(ValidationError):
        ChangeScheduleEdit(op="change_schedule", habit_id="a", cue="07:30", days=["mon", "mon"])


def test_cue_loss_habits_only_reports_removed_anchors():
    active = [ACTIVE, {"habit_id": "b", "anchor": "morning"}]
    assert cue_loss_habits({"bedtime": {}, "morning": {}}, {"morning": {}}, active) == ["a"]


WEAK = make_habit("weak", goals={"g": 0.5, "h": 1.0})
WEAK_DATA = {
    **DATA,
    "goals": [{"goal_id": "g", "status": "covered"}, {"goal_id": "h", "status": "covered"}],
    "active": [ACTIVE, {**entry(WEAK, ["g", "h"], WEAK.levels[0], PROFILE), "covers": ["g", "h"]}],
    "shortlists": {"g": [{"habit_id": "a"}, {"habit_id": "weak"}], "h": [{"habit_id": "weak"}]},
    "chapters": [*DATA["chapters"], {"id": "space-h", "version": 1, "goal_id": "h", "title": "H"}],
}


def test_weak_links_stop_covering_once_an_alternative_is_demoted():
    """Like clear-plate for meals: a weak link covers a goal only while its alternatives are active too."""
    release = replace(RELEASE, habits={**HABITS, "weak": WEAK})
    with pytest.raises(ValueError, match=r"coverage: \['g'\]"):
        patch(data=WEAK_DATA, release=release, op="demote_active", habit_id="a")
    kept = patch(
        data=WEAK_DATA, release=release, op="change_schedule", habit_id="a", cue="07:00", days=["mon"]
    )
    assert [h["covers"] for h in kept["active"]] == [["g"], ["g", "h"]]


def test_a_demoted_future_alternative_also_withdraws_weak_coverage():
    """Second intakes leave carried habits out of the shortlists; the demoted habit is found in future."""
    release = replace(RELEASE, habits={**HABITS, "weak": WEAK})
    data = {**WEAK_DATA, "shortlists": {}}
    with pytest.raises(ValueError, match=r"coverage: \['g'\]"):
        patch(data=data, release=release, op="demote_active", habit_id="a")
    # A future alternative without an agreed cue does not count: the weak link still covers.
    evening = {**data, "future": [{"habit_id": "e", "version": 1, "goal_ids": ["g"], "score": 1}]}
    kept = patch(data=evening, release=release, op="change_schedule", habit_id="a", cue="07:00", days=["mon"])
    assert kept["active"][1]["covers"] == ["g", "h"]


def test_entries_missing_from_the_release_keep_their_recorded_coverage():
    """Confirmation revalidates them against the release; an edit does not silently drop their goals."""
    release = replace(RELEASE, habits={k: v for k, v in HABITS.items() if k != "a"})
    kept = patch(
        data=WEAK_DATA, release=release, op="change_schedule", habit_id="a", cue="07:00", days=["mon"]
    )
    assert kept["active"][0]["covers"] == ["g"]
