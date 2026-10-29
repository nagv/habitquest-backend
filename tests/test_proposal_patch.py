import pytest
from conftest import CONFIRM, queued

from habitquest_backend import worker


def ready(client, settings):
    child, record, run = queued(client)
    worker.run_once(settings)
    assert client.get(f"/v1/plan-runs/{run}").json()["status"] == "ready"
    return child, record, run


def patch(client, run, op_body, *, version=1):
    body = {"edit": op_body, "expected_proposal_version": version}
    return client.patch(f"/v1/proposals/{run}", json=body)


def test_change_level_updates_proposal_version(client, settings):
    _, _, run = ready(client, settings)
    proposal = client.get(f"/v1/proposals/{run}").json()
    first_active = proposal["data"]["active"][0]
    current_level = first_active["level"]
    new_level = "simple" if current_level != "simple" else "standard"
    resp = patch(client, run, {"op": "change_level", "habit_id": first_active["habit_id"], "level": new_level})
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated["version"] == 2
    assert any(h["level"] == new_level for h in updated["data"]["active"])


def test_stale_version_returns_409(client, settings):
    _, _, run = ready(client, settings)
    resp = patch(client, run, {"op": "demote_active", "habit_id": "bedtime-prep"}, version=99)
    assert resp.status_code == 409


def test_remove_goal_drops_it_from_proposal(client, settings):
    _, _, run = ready(client, settings)
    resp = patch(client, run, {"op": "remove_goal", "goal_id": "meals"})
    assert resp.status_code == 200
    goals = [g["goal_id"] for g in resp.json()["data"]["goals"]]
    assert "meals" not in goals


def test_promote_future_validates_against_current_release(client, settings, db):
    _, _, run = ready(client, settings)
    data = client.get(f"/v1/proposals/{run}").json()["data"]
    if not data["future"]:
        pytest.skip("No future candidates in this proposal")
    future_id = data["future"][0]["habit_id"]
    db.execute(
        "UPDATE hq.habit_versions SET withdrawn_at=now(),withdrawn_reason='test' WHERE habit_id=%s",
        (future_id,),
    )
    resp = patch(client, run, {"op": "promote_future", "habit_id": future_id, "level": "standard"})
    assert resp.status_code == 422


def test_demote_active_moves_habit_to_future(client, settings):
    _, _, run = ready(client, settings)
    data = client.get(f"/v1/proposals/{run}").json()["data"]
    active_id = data["active"][0]["habit_id"]
    resp = patch(client, run, {"op": "demote_active", "habit_id": active_id})
    assert resp.status_code == 200
    result = resp.json()["data"]
    assert not any(h["habit_id"] == active_id for h in result["active"])
    assert any(h["habit_id"] == active_id for h in result["future"])


def test_swap_habit_within_shortlist(client, settings):
    _, _, run = ready(client, settings)
    data = client.get(f"/v1/proposals/{run}").json()["data"]
    active = data["active"][0]
    goal_id = active["goal_ids"][0]
    options = data["shortlists"].get(goal_id, [])
    alternatives = [o for o in options if o["habit_id"] != active["habit_id"]]
    if not alternatives:
        pytest.skip("No alternative in shortlist for this goal")
    new_id = alternatives[0]["habit_id"]
    resp = patch(
        client, run, {"op": "swap_habit", "old_habit_id": active["habit_id"], "new_habit_id": new_id, "level": "standard"}
    )
    assert resp.status_code == 200


def test_confirmed_proposal_cannot_be_patched(client, settings):
    _, _, run = ready(client, settings)
    client.post(f"/v1/proposals/{run}/confirm", json=CONFIRM)
    resp = patch(client, run, {"op": "demote_active", "habit_id": "bedtime-prep"})
    assert resp.status_code == 409
