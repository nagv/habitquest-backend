from dataclasses import replace

import pytest
from catalog_factories import make_habit
from pydantic import ValidationError

from habitquest_backend.catalog.models import Chapter, Concern, Goal, Release
from habitquest_backend.catalog.releases import diff_items, validate_release
from habitquest_backend.editorial.checks import goal_findings, habit_findings, lint
from habitquest_backend.editorial.schemas import ConcernDraft, HabitDraft


def chapters(*goals):
    return {(w, g): Chapter(f"{w}-{g}", 1, w, g, "T") for g in goals for w in ("space", "forest", "city")}


def test_valid_release_has_no_errors_or_warnings():
    release = Release(
        1,
        {"g": Goal("g", 1, "G", "")},
        {"c": Concern("c", 1, "C", (), {"g": 1.0})},
        {"h": make_habit("h")},
        chapters("g"),
    )
    assert validate_release(release) == {"errors": [], "warnings": []}


def test_release_errors_and_age_band_warnings():
    habits = {
        "young": make_habit(
            "young", goals={"g": 1.0, "ghost": 0.5}, max_age=9, supports={"gone": "prepares"}
        ),
        "old": make_habit("old", withdrawn=True),
        "empty": replace(make_habit("empty"), levels=()),
    }
    release = Release(
        1,
        {"g": Goal("g", 1, "G", "")},
        {"none": Concern("none", 1, "N", (), {}), "stray": Concern("stray", 1, "S", (), {"ghost": 1.0})},
        habits,
        {**chapters("g"), ("space", "ghost"): Chapter("space-ghost", 1, "space", "ghost", "T")},
    )
    del release.chapters[("city", "g")]
    report = validate_release(release)
    assert report["errors"] == [
        "missing_chapter:city:g",
        "chapter_unknown_goal:space-ghost:ghost",
        "habit_without_levels:empty",
        "habit_withdrawn:old",
        "habit_unknown_goal:young:ghost",
        "habit_unknown_prereq:young:gone",
        "concern_without_goal:none",
        "concern_unknown_goal:stray:ghost",
    ]
    assert report["warnings"] == ["no_habit_for_age_band:g:10-13"]


def test_diff_items():
    base = {("habit", "a"): 1, ("habit", "b"): 1, ("goal", "g"): 2}
    candidate = {("habit", "a"): 2, ("goal", "g"): 2, ("chapter", "c"): 1}
    changes = diff_items(candidate, base)
    assert changes["habit"] == {
        "added": [],
        "removed": [{"id": "b", "version": 1}],
        "changed": [{"id": "a", "from": 1, "to": 2}],
    }
    assert changes["chapter"]["added"] == [{"id": "c", "version": 1}]
    assert changes["goal"] == {"added": [], "removed": [], "changed": []}


def test_habit_findings_recheck_stored_rows():
    record = {
        "title": "Tidy",
        "instruction": "Put things away.",
        "levels": [],
        "min_age": 10,
        "max_age": 8,
        "goals": {},
        "variants": {"art": {"title": "Studio", "instruction": "Use the stove."}},
    }
    assert habit_findings(record, set()) == ["levels_count", "age_bounds", "no_goal", "dangerous_activity"]
    record = {
        **record,
        "levels": [{"instruction": "Take your ADHD medication."}],
        "min_age": 6,
        "goals": {"g": 1},
        "variants": {},
    }
    assert habit_findings(record, {"g"}) == ["diagnosis_treatment"]


def test_lint_codes():
    assert goal_findings({"title": "Keep it between us", "description": "Don't tell anyone"}, set()) == [
        "secrecy"
    ]
    assert lint("Brush your teeth at the agreed cue.") == []


LEVEL = {"level": "standard", "minutes": 5, "points": 1}
HABIT = {"title": "T", "instruction": "I", "min_age": 6, "max_age": 13, "routine_anchor": "evening"}


@pytest.mark.parametrize(
    ("model", "body"),
    [
        (HabitDraft, {**HABIT, "min_age": 10, "max_age": 8, "levels": [LEVEL], "goals": {"g": 1}}),
        (HabitDraft, {**HABIT, "levels": [LEVEL, LEVEL], "goals": {"g": 1}}),
        (HabitDraft, {**HABIT, "levels": [LEVEL], "goals": {"g": 1}, "tags": ["access:x", "access:x"]}),
        (ConcernDraft, {"label": "L", "synonyms": ["a", "a"], "goals": {"g": 1}}),
    ],
)
def test_draft_schemas_reject_invalid_bodies(model, body):
    with pytest.raises(ValidationError):
        model(**body)
