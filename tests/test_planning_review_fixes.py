from dataclasses import replace

import pytest
from catalog_factories import make_habit, make_release
from conftest import ALEX, ALEX_CONCERNS, CONFIRM, intake, queued

from habitquest_backend import planning, worker
from habitquest_backend.catalog.models import ChildProfile, Level, RoutineSlot
from habitquest_backend.planning import CAP_RATIONALE, revalidate_carried

SECOND = {"expected_plan_version": 1, "expected_proposal_version": 1}


def plan_for(client, settings, child, key, **body):
    _, record = intake(client, child=child, **body)
    response = client.post(f"/v1/intakes/{record['id']}/plan-runs", headers={"Idempotency-Key": key})
    worker.run_once(settings)
    return record, response.json()["run_id"]


def confirmed_alex(client, settings, **overrides):
    child, _, run = queued(client, **overrides)
    worker.run_once(settings)
    assert client.post(f"/v1/proposals/{run}/confirm", json=CONFIRM).status_code == 200
    return child


@pytest.mark.parametrize(
    "mapping",
    [
        {"goals": [{"goal_id": "bedtime", "evidence_span": "sleep schedule", "confidence": 1}]},
        {"goals": [{"goal_id": "bedtime", "evidence_span": "", "confidence": 1}]},
        {
            "goals": [{"goal_id": "bedtime", "evidence_span": "BEDTIME", "confidence": 1}],
            "unmatched_phrases": ["something the parent never said"],
        },
    ],
)
def test_mapping_must_quote_the_parent(client, settings, monkeypatch, mapping):
    _, _, run = queued(client)
    monkeypatch.setattr(planning, "fake_mapping", lambda concerns: {**mapping, "explanation": "Quoted"})
    worker.run_once(settings)
    assert client.get(f"/v1/plan-runs/{run}").json()["status"] == "failed"


def test_case_insensitive_quotes_are_accepted(client, settings, monkeypatch):
    _, _, run = queued(client)
    monkeypatch.setattr(
        planning,
        "fake_mapping",
        lambda concerns: {
            "goals": [{"goal_id": "bedtime", "evidence_span": "AT BEDTIME", "confidence": 1}],
            "unmatched_phrases": ["MORNINGS ARE RUSHED"],
            "explanation": "Quoted",
        },
    )
    worker.run_once(settings)
    assert client.get(f"/v1/plan-runs/{run}").json()["status"] == "ready"


def test_withdrawn_carried_habit_is_dropped_on_the_next_plan(client, settings, db):
    child = confirmed_alex(client, settings)
    db.execute(
        "UPDATE hq.habit_versions SET withdrawn_at=now(),withdrawn_reason='safety' WHERE habit_id='clear-plate'"
    )
    _, run = plan_for(client, settings, child, "second", concerns=ALEX_CONCERNS)
    data = client.get(f"/v1/proposals/{run}").json()["data"]
    assert data["changes"] == [{"op": "drop", "habit_id": "clear-plate", "version": 1, "reason": "withdrawn"}]
    assert [h["habit_id"] for h in data["active"]] == ["bedtime-prep", "join-meal"]
    assert [h["covers"] for h in data["active"]] == [["bedtime"], ["meals"]]
    assert {g["goal_id"]: g["status"] for g in data["goals"]} == {
        "bedtime": "covered",
        "morning": "needs_cue",
        "responsibility": "needs_cue",
        "meals": "covered",
    }
    plan = client.post(f"/v1/proposals/{run}/confirm", json=SECOND)
    assert plan.status_code == 200, plan.text
    assert plan.json()["version"] == 2


def test_available_minutes_is_the_daily_total(client, settings):
    child = confirmed_alex(client, settings, concerns="Bedtime is hard")
    plan = client.get(f"/v1/children/{child['id']}/plan").json()
    assert [(h["habit_id"], h["minutes"], h["added_in_plan_version"]) for h in plan["data"]["active"]] == [
        ("bedtime-prep", 10, 1)
    ]
    _, run = plan_for(
        client,
        settings,
        child,
        "second",
        concerns="Plates are left out and dinner is a struggle",
        available_minutes=12,
    )
    data = client.get(f"/v1/proposals/{run}").json()["data"]
    # 12 - 10 carried leaves 2 minutes: only clear-plate (2) fits; join-meal (5) is not eligible, so the
    # weak meals link of clear-plate has no remaining dedicated alternative and covers meals.
    assert [(h["habit_id"], h["added_in_plan_version"]) for h in data["active"]] == [
        ("bedtime-prep", 1),
        ("clear-plate", 2),
    ]
    assert data["changes"] == [{"op": "add", "habit_id": "clear-plate", "version": 1, "level": "standard"}]
    assert data["active"][1]["covers"] == ["meals", "responsibility"]
    assert {g["goal_id"]: g["status"] for g in data["goals"]} == {
        "responsibility": "covered",
        "meals": "covered",
    }
    assert "join-meal" not in {h["habit_id"] for h in data["future"]}
    assert sum(h["minutes"] for h in data["active"]) == 12


def test_profile_change_invalidates_the_proposal(client, settings):
    child, _, run = queued(client)
    worker.run_once(settings)
    assert client.get(f"/v1/proposals/{run}").json()["data"]["profile_version"] == 1
    patched = client.patch(f"/v1/children/{child['id']}", json={**ALEX, "expected_profile_version": 1})
    assert patched.status_code == 200
    response = client.post(f"/v1/proposals/{run}/confirm", json=CONFIRM)
    assert response.status_code == 409
    assert response.json()["detail"] == "Profile changed; request a new proposal"


def test_profile_change_during_planning_blocks_publication(client, settings, db, monkeypatch):
    _, _, run = queued(client)
    original = worker.generate

    def edit(*args):
        data = original(*args)
        db.execute("UPDATE hq.children SET profile_version=profile_version+1")
        return data

    monkeypatch.setattr(worker, "generate", edit)
    worker.run_once(settings)
    assert client.get(f"/v1/plan-runs/{run}").json()["status"] == "failed"
    assert db.execute("SELECT count(*) FROM hq.proposals").fetchone()[0] == 0


REPUBLISH = (
    "INSERT INTO hq.catalog_releases(id,built_by) VALUES(2,'00000000-0000-0000-0000-0000000000a1');"
    "INSERT INTO hq.release_items SELECT 2,entity,entity_id,version FROM hq.release_items WHERE release_id=1;"
    "UPDATE hq.catalog_releases SET is_current=false WHERE id=1;"
    "UPDATE hq.catalog_releases SET published_by='00000000-0000-0000-0000-0000000000a2',"
    "published_at=now(),is_current=true WHERE id=2"
)


def test_confirmation_records_the_release_it_checked(client, settings, db):
    _, _, run = queued(client)
    worker.run_once(settings)
    assert client.get(f"/v1/proposals/{run}").json()["release_id"] == 1
    db.execute(REPUBLISH)
    response = client.post(f"/v1/proposals/{run}/confirm", json=CONFIRM)
    assert response.status_code == 200, response.text
    assert response.json()["release_id"] == 2


def test_raised_again_goal_is_reactivated(client, settings, db):
    child = confirmed_alex(client, settings)
    db.execute("UPDATE hq.child_goals SET status='resolved' WHERE goal_id='bedtime'")
    record, run = plan_for(client, settings, child, "second", concerns="Bedtime is still difficult")
    assert client.post(f"/v1/proposals/{run}/confirm", json=SECOND).status_code == 200
    goals = {g["goal_id"]: g for g in client.get(f"/v1/children/{child['id']}/goals").json()}
    assert (goals["bedtime"]["status"], goals["bedtime"]["since_plan_version"]) == ("active", 2)
    assert goals["bedtime"]["source_intake_id"] == record["id"]
    assert (goals["meals"]["status"], goals["meals"]["since_plan_version"]) == ("active", 1)


def test_legacy_plan_carries_nothing(client, settings, db):
    child = confirmed_alex(client, settings)
    db.execute(
        "UPDATE hq.plans SET release_id=NULL,data=jsonb_set(data,'{active}',"
        """'[{"habit_id":"legacy-habit","version":1,"goal_ids":["bedtime"],"minutes":10}]')"""
    )
    _, run = plan_for(client, settings, child, "second", concerns=ALEX_CONCERNS)
    proposal = client.get(f"/v1/proposals/{run}").json()
    assert proposal["base_plan_version"] == 1
    data = proposal["data"]
    assert [h["habit_id"] for h in data["active"]] == ["bedtime-prep", "clear-plate", "join-meal"]
    assert {c["op"] for c in data["changes"]} == {"add"}
    assert data["rationale"] != CAP_RATIONALE
    plan = client.post(f"/v1/proposals/{run}/confirm", json=SECOND)
    assert plan.status_code == 200 and plan.json()["version"] == 2


PROFILE = ChildProfile(age_band="6-9", routine_schedule={"bedtime": RoutineSlot("20:00", ("mon",))})


def old(habit_id, version=1, level="standard", **extra):
    return {"habit_id": habit_id, "version": version, "level": level, "goal_ids": ["g"], **extra}


def test_revalidate_carried_drops_and_upgrades():
    upgraded = make_habit("up", levels=(Level("standard", 4, 2, "Newer wording."),))
    release = make_release(
        make_habit("kept"),
        make_habit("gone", withdrawn=True),
        make_habit("less", levels=(Level("simple", 2, 1),)),
        make_habit("cue", anchor="morning"),
        replace(upgraded, version=3),
    )
    carried = [
        old("kept", covers=["g"], added_in_plan_version=2),
        old("missing"),
        old("gone"),
        old("less"),
        old("cue"),
        old("up"),
    ]
    kept, changes = revalidate_carried(carried, release, PROFILE, base_version=4)
    assert changes == [
        {"op": "drop", "habit_id": "missing", "version": 1, "reason": "not_in_release"},
        {"op": "drop", "habit_id": "gone", "version": 1, "reason": "withdrawn"},
        {"op": "drop", "habit_id": "less", "version": 1, "reason": "level_removed"},
        {"op": "drop", "habit_id": "cue", "version": 1, "reason": "needs_cue"},
        {"op": "upgrade", "habit_id": "up", "from_version": 1, "to_version": 3},
    ]
    assert [(h["habit_id"], h["version"], h["covers"], h["added_in_plan_version"]) for h in kept] == [
        ("kept", 1, ["g"], 2),
        ("up", 3, ["g"], 4),
    ]
    assert (kept[1]["instruction"], kept[1]["minutes"], kept[1]["cue"]) == ("Newer wording.", 4, "20:00")
