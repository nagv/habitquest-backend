import pytest
from catalog_factories import make_habit, make_release
from pydantic import ValidationError

from habitquest_backend.catalog.models import Level
from habitquest_backend.schemas import ChangeScheduleEdit
from habitquest_backend.service import apply_patch, cue_loss_habits

RELEASE = make_release(
    make_habit("a"),
    make_habit("b"),
    make_habit("f", levels=(Level("simple", 3, 1), Level("standard", 6, 2))),
    make_habit("w", withdrawn=True),
)
ACTIVE = {"habit_id": "a", "version": 1, "goal_ids": ["g"], "level": "standard", "anchor": "bedtime"}
DATA = {
    "goals": [{"goal_id": "g"}, {"goal_id": "h"}],
    "active": [ACTIVE],
    "future": [
        {"habit_id": "f", "version": 1, "goal_ids": ["g"]},
        {"habit_id": "w", "version": 1, "goal_ids": ["g"]},
    ],
    "shortlists": {"g": [{"habit_id": "a"}, {"habit_id": "b"}]},
}


def patch(**edit):
    return apply_patch(DATA, edit, RELEASE)


def test_reorder_goals_requires_every_goal_once():
    assert [g["goal_id"] for g in patch(op="reorder_goals", goal_ids=["h", "g"])["goals"]] == ["h", "g"]
    with pytest.raises(ValueError, match="every proposal goal"):
        patch(op="reorder_goals", goal_ids=["h"])
    with pytest.raises(ValueError, match="every proposal goal"):
        patch(op="reorder_goals", goal_ids=["h", "g", "x"])


def test_change_level_and_schedule():
    assert patch(op="change_level", habit_id="a", level="simple")["active"][0]["level"] == "simple"
    changed = patch(op="change_schedule", habit_id="a", cue="07:30", days=["mon"])["active"][0]
    assert (changed["cue"], changed["days"]) == ("07:30", ["mon"])
    assert DATA["active"][0] == ACTIVE


def test_swap_habit_within_shortlist():
    assert patch(op="swap_habit", old_habit_id="a", new_habit_id="b", level="simple")["active"][0] == {
        **ACTIVE,
        "habit_id": "b",
        "level": "simple",
    }
    with pytest.raises(ValueError, match="not in the shortlist"):
        patch(op="swap_habit", old_habit_id="a", new_habit_id="f", level="simple")


def test_promote_future_uses_release_level():
    result = patch(op="promote_future", habit_id="f", level="simple")
    assert [h["habit_id"] for h in result["future"]] == ["w"]
    assert result["active"][-1]["minutes"] == 3
    with pytest.raises(ValueError, match="no 'stretch' level"):
        patch(op="promote_future", habit_id="f", level="stretch")
    with pytest.raises(ValueError, match="stale"):
        patch(op="promote_future", habit_id="w", level="standard")


@pytest.mark.parametrize(
    "edit",
    [
        {"op": "change_level", "habit_id": "x", "level": "simple"},
        {"op": "change_schedule", "habit_id": "x", "cue": "07:30", "days": ["mon"]},
        {"op": "swap_habit", "old_habit_id": "x", "new_habit_id": "b", "level": "simple"},
        {"op": "demote_active", "habit_id": "x"},
        {"op": "promote_future", "habit_id": "x", "level": "simple"},
        {"op": "invented"},
    ],
)
def test_unknown_targets_are_rejected(edit):
    with pytest.raises(ValueError):
        apply_patch(DATA, edit, RELEASE)


def test_change_schedule_rejects_duplicate_days():
    with pytest.raises(ValidationError):
        ChangeScheduleEdit(op="change_schedule", habit_id="a", cue="07:30", days=["mon", "mon"])


def test_cue_loss_habits_only_reports_removed_anchors():
    active = [ACTIVE, {"habit_id": "b", "anchor": "morning"}]
    assert cue_loss_habits({"bedtime": {}, "morning": {}}, {"morning": {}}, active) == ["a"]
