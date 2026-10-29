import importlib

import pytest
from conftest import CONFIRM, OTHER, intake, queued, token


def test_other_household_cannot_read_or_use_child_or_intake(client, settings):
    child, record = intake(client)
    client.headers["Authorization"] = "Bearer " + token(settings, OTHER)
    assert client.get(f"/v1/children/{child['id']}").status_code == 404
    update = {"name": "X", "age_band": "6-9", "expected_profile_version": 1}
    assert client.patch(f"/v1/children/{child['id']}", json=update).status_code == 404
    assert (
        client.post(f"/v1/children/{child['id']}/intakes", json={"concerns": "Bedtime is hard"}).status_code
        == 404
    )
    assert (
        client.post(f"/v1/intakes/{record['id']}/plan-runs", headers={"Idempotency-Key": "x"}).status_code
        == 404
    )
    assert client.get(f"/v1/children/{child['id']}/goals").status_code == 404


def test_duplicate_job_creation_is_idempotent(client):
    _, record, run = queued(client)
    again = client.post(f"/v1/intakes/{record['id']}/plan-runs", headers={"Idempotency-Key": "first"})
    assert again.json()["run_id"] == run
    assert client.get(f"/v1/plan-runs/{run}").json()["status"] == "queued"


def test_alex_example_from_concerns_to_confirmed_plan(client, settings, monkeypatch):
    _, _, run = queued(client)
    try:
        worker = importlib.import_module("habitquest_backend.worker")
    except ModuleNotFoundError:
        pytest.fail("Durable agent worker has not been implemented")
    assert worker.run_once(settings)
    assert client.get(f"/v1/plan-runs/{run}").json()["status"] == "ready"
    proposal = client.get(f"/v1/proposals/{run}").json()
    data = proposal["data"]
    assert proposal["version"] == 1 and proposal["release_id"] == 1
    assert [g["goal_id"] for g in data["goals"]] == ["bedtime", "morning", "responsibility", "meals"]
    assert data["goals"][3]["suggested_not_stated"] and data["goals"][3]["status"] == "covered"
    assert data["goals"][1]["status"] == "needs_cue"
    assert [h["habit_id"] for h in data["active"]] == ["bedtime-prep", "clear-plate", "join-meal"]
    assert data["active"][0]["title"] == "Prepare for Night Docking" and data["active"][0]["cue"] == "20:00"
    assert sorted(h["habit_id"] for h in data["future"]) == ["morning-ready", "pack-bag"]
    assert {"habit_id": "pack-bag", "supports": "morning-ready", "kind": "prepares"} in data["routine_links"]
    assert [c["title"] for c in data["chapters"]] == [
        "Prepare the Home Base",
        "Prepare Your Equipment",
        "Join the Crew",
    ]
    assert any("morning cue" in q for q in data["questions"])
    assert not data["fallback"] and data["review"]["accepted"]
    response = client.post(f"/v1/proposals/{run}/confirm", json=CONFIRM)
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 1
    assert client.post(f"/v1/proposals/{run}/confirm", json=CONFIRM).json()["id"] == response.json()["id"]
