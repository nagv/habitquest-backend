import importlib

import pytest
from conftest import OTHER, onboard, queued, token


def test_clarification_blocks_unconfirmed_or_incomplete_goals(client):
    child, session = onboard(client, bedtime=None)
    assert len(session["questions"]) == 1
    assert (
        client.post(
            f"/v1/onboarding/{session['id']}/confirm-goals",
            json={"expected_version": 1, "goals": ["bedtime"]},
        ).status_code
        == 409
    )
    assert (
        client.post(f"/v1/onboarding/{session['id']}/plan-runs", headers={"Idempotency-Key": "x"}).status_code
        == 409
    )


def test_other_household_cannot_read_or_mutate_child_session(client, settings):
    child, session = onboard(client)
    client.headers["Authorization"] = "Bearer " + token(settings, OTHER)
    assert client.get(f"/v1/children/{child['id']}/preferences").status_code == 404
    assert (
        client.post(
            f"/v1/onboarding/{session['id']}/confirm-goals",
            json={"expected_version": 1, "goals": ["bedtime"]},
        ).status_code
        == 404
    )


def test_duplicate_job_creation_is_idempotent(client):
    _, session, run = queued(client)
    assert (
        client.post(f"/v1/onboarding/{session['id']}/plan-runs", headers={"Idempotency-Key": "first"}).json()[
            "run_id"
        ]
        == run
    )
    assert client.get(f"/v1/plan-runs/{run}").json()["status"] == "queued"


def test_full_goal_to_confirmed_plan_through_real_agent_and_database(client, settings, monkeypatch):
    _, _, run = queued(client)
    try:
        worker = importlib.import_module("habitquest_backend.worker")
    except ModuleNotFoundError:
        pytest.fail("Durable agent worker has not been implemented")
    original = worker.generate

    def diagnose(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except Exception:
            import traceback

            traceback.print_exc()
            raise

    monkeypatch.setattr(worker, "generate", diagnose)
    assert worker.run_once(settings)
    assert client.get(f"/v1/plan-runs/{run}").json()["status"] == "ready"
    proposal = client.get(f"/v1/proposals/{run}").json()
    assert {x["id"] for x in proposal["data"]["habits"]} == {"bedtime-prep", "morning-ready", "clear-plate"}
    assert proposal["data"]["chapters"][0]["title"] == "Prepare the Home Base"
    response = client.post(f"/v1/proposals/{run}/confirm", json={"expected_plan_version": 0})
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 1
    assert (
        client.post(f"/v1/proposals/{run}/confirm", json={"expected_plan_version": 0}).json()["id"]
        == response.json()["id"]
    )
