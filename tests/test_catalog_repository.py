import pytest
from fastapi import HTTPException

from habitquest_backend.catalog.models import Level
from habitquest_backend.catalog.repository import load_release
from habitquest_backend.db import transaction


def test_load_current_release(settings, db):
    with transaction(settings, worker=True) as conn:
        release = load_release(conn)
    assert release.id == 1
    assert set(release.goals) == {"bedtime", "morning", "bedtime-mornings", "meals", "responsibility"}
    plate = release.habits["clear-plate"]
    assert plate.goals == {"responsibility": 1.0, "meals": 0.5}
    assert plate.levels == (Level("standard", 2, 1),)
    assert plate.routine_anchor == "after_school"
    assert plate.tags == frozenset({"access:short_instructions"})
    assert not plate.withdrawn
    bedtime = release.habits["bedtime-prep"]
    assert bedtime.variants["space"] == (
        "Prepare for Night Docking",
        "At the agreed cue, put on pajamas and brush your teeth.",
    )
    assert release.habits["pack-bag"].supports == {"morning-ready": "prepares"}
    assert release.concerns["belongings-left"].goals == {"responsibility": 1.0}
    assert "plates left out" in release.concerns["belongings-left"].synonyms
    assert release.chapters[("space", "bedtime")].title == "Prepare the Home Base"


def test_load_named_release_without_a_current_release(settings, db):
    db.execute("UPDATE hq.catalog_releases SET is_current=false")
    with transaction(settings, worker=True) as conn:
        assert load_release(conn, 1).id == 1
        with pytest.raises(HTTPException) as error:
            load_release(conn)
    assert error.value.status_code == 404


def test_presented_wording_prefers_matching_interest_variant(settings, db):
    with transaction(settings, worker=True) as conn:
        habit = load_release(conn).habits["bedtime-prep"]
    assert habit.presented(frozenset({"animals", "space"}))[0] == "Prepare for Night Docking"
    assert habit.presented(frozenset({"animals"})) == (habit.title, habit.instruction)
