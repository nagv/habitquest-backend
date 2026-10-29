import json

from conftest import ALEX, CONFIRM, intake, queued
from langchain_core.messages import AIMessage, ToolMessage

from habitquest_backend import planning, worker


def test_profile_round_trip_and_versioned_update(client):
    child = client.post("/v1/children", json=ALEX).json()
    assert child["interests"] == ["space"] and child["profile_version"] == 1
    fetched = client.get(f"/v1/children/{child['id']}").json()
    assert fetched["timezone"] == "America/New_York"
    assert fetched["routine_schedule"]["bedtime"]["cue"] == "20:00"
    update = {**ALEX, "preferred_world": "forest", "timezone": "Europe/London", "expected_profile_version": 1}
    response = client.patch(f"/v1/children/{child['id']}", json=update)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["preferred_world"], body["profile_version"], body["timezone"]) == (
        "forest",
        2,
        "Europe/London",
    )
    assert client.patch(f"/v1/children/{child['id']}", json=update).status_code == 409


def test_goal_mapping_outside_the_release_fails_closed(client, settings, monkeypatch):
    _, _, run = queued(client)
    monkeypatch.setattr(
        planning,
        "fake_mapping",
        lambda concerns: {
            "goals": [{"goal_id": "invented", "evidence_span": "x", "confidence": 1}],
            "explanation": "An invented goal",
        },
    )
    worker.run_once(settings)
    assert client.get(f"/v1/plan-runs/{run}").json()["status"] == "failed"


def test_stated_meals_are_not_marked_as_suggested(client, settings):
    _, _, run = queued(client, concerns="Dinner is hard and plates are left out")
    worker.run_once(settings)
    goals = client.get(f"/v1/proposals/{run}").json()["data"]["goals"]
    assert [(g["goal_id"], g["suggested_not_stated"]) for g in goals] == [
        ("meals", False),
        ("responsibility", False),
    ]


def test_invalid_pick_is_revised_then_published(client, settings, db, monkeypatch):
    _, _, run = queued(client)
    original = planning.BoundedChat._fake
    calls = []

    def once(self, messages):
        if self.role == "planner" and any(isinstance(m, ToolMessage) for m in messages) and not calls:
            calls.append(1)
            return AIMessage(
                content=json.dumps(
                    {
                        "picks": [{"habit_id": "invented", "level": "standard"}],
                        "rationale": "An invalid first attempt",
                        "order_explanation": "No particular order",
                    }
                )
            )
        return original(self, messages)

    monkeypatch.setattr(planning.BoundedChat, "_fake", once)
    worker.run_once(settings)
    data = client.get(f"/v1/proposals/{run}").json()["data"]
    assert not data["fallback"] and data["review"]["accepted"]
    assert db.execute("SELECT count(*) FROM hq.events WHERE kind='validation'").fetchone()[0] == 1


def test_second_intake_carries_the_confirmed_plan(client, settings):
    child, _, run = queued(client)
    worker.run_once(settings)
    assert client.post(f"/v1/proposals/{run}/confirm", json=CONFIRM).status_code == 200
    _, second = intake(
        client,
        child=child,
        concerns="Bedtime is still difficult and mornings are rushed",
        available_minutes=10,
    )
    run2 = client.post(f"/v1/intakes/{second['id']}/plan-runs", headers={"Idempotency-Key": "second"}).json()[
        "run_id"
    ]
    worker.run_once(settings)
    proposal = client.get(f"/v1/proposals/{run2}").json()
    data = proposal["data"]
    assert proposal["base_plan_version"] == 1 and data["changes"] == []
    assert [h["habit_id"] for h in data["active"]] == ["bedtime-prep", "clear-plate", "join-meal"]
    assert {g["goal_id"]: g["status"] for g in data["goals"]} == {
        "bedtime": "covered",
        "morning": "needs_cue",
    }
    plan = client.post(
        f"/v1/proposals/{run2}/confirm", json={"expected_plan_version": 1, "expected_proposal_version": 1}
    ).json()
    assert plan["version"] == 2 and len(plan["data"]["chapters"]) == 3
