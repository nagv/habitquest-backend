import pytest
from conftest import ALEX, CONFIRM, queued

from habitquest_backend import worker


def ready(client, settings):
    child, record, run = queued(client)
    worker.run_once(settings)
    assert client.get(f"/v1/plan-runs/{run}").json()["status"] == "ready"
    return child, record, run


def patch(client, run, op_body, *, version=1):
    body = {"edit": op_body, "expected_proposal_version": version}
    return client.patch(f"/v1/proposals/{run}", json=body)


def test_edit_increments_proposal_version(client, settings):
    _, _, run = ready(client, settings)
    habit_id = client.get(f"/v1/proposals/{run}").json()["data"]["active"][0]["habit_id"]
    days = ["mon", "wed"]
    resp = patch(client, run, {"op": "change_schedule", "habit_id": habit_id, "cue": "19:45", "days": days})
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == 2
    assert (resp.json()["data"]["active"][0]["cue"], resp.json()["data"]["active"][0]["days"]) == (
        "19:45",
        days,
    )


def test_change_level_rejects_levels_the_habit_lacks(client, settings):
    _, _, run = ready(client, settings)
    resp = patch(client, run, {"op": "change_level", "habit_id": "bedtime-prep", "level": "simple"})
    assert (resp.status_code, resp.json()["detail"]) == (422, "level: 'bedtime-prep' has no 'simple' level")


def test_promote_is_checked_against_cues_and_limits(client, settings):
    """The local walk-through: pack-bag needs an evening cue Alex does not have, and the plan is full."""
    _, _, run = ready(client, settings)
    data = client.get(f"/v1/proposals/{run}").json()["data"]
    assert [h["habit_id"] for h in data["future"]] == ["pack-bag", "morning-ready"]
    resp = patch(client, run, {"op": "promote_future", "habit_id": "pack-bag", "level": "standard"})
    assert resp.status_code == 422
    assert resp.json()["detail"].startswith("needs_cue: add a evening cue")
    resp = patch(client, run, {"op": "promote_future", "habit_id": "morning-ready", "level": "standard"})
    assert resp.status_code == 422 and resp.json()["detail"].startswith("needs_cue: add a morning cue")


def test_edit_after_profile_change_returns_409(client, settings):
    child, _, run = ready(client, settings)
    profile = client.get(f"/v1/children/{child['id']}").json()
    body = {**ALEX, "expected_profile_version": profile["profile_version"]}
    assert client.patch(f"/v1/children/{child['id']}", json=body).status_code == 200
    resp = patch(client, run, {"op": "remove_goal", "goal_id": "meals"})
    assert resp.status_code == 409


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
    assert resp.status_code == 422 and resp.json()["detail"].startswith("coverage:")
    covered = data["active"][0]["covers"]
    for version, goal_id in enumerate(covered, start=1):
        assert (
            patch(client, run, {"op": "remove_goal", "goal_id": goal_id}, version=version).status_code == 200
        )
    resp = patch(client, run, {"op": "demote_active", "habit_id": active_id}, version=len(covered) + 1)
    assert resp.status_code == 200, resp.text
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
        client,
        run,
        {"op": "swap_habit", "old_habit_id": active["habit_id"], "new_habit_id": new_id, "level": "standard"},
    )
    assert resp.status_code == 200


def test_confirmed_proposal_cannot_be_patched(client, settings):
    _, _, run = ready(client, settings)
    client.post(f"/v1/proposals/{run}/confirm", json=CONFIRM)
    resp = patch(client, run, {"op": "demote_active", "habit_id": "bedtime-prep"})
    assert resp.status_code == 409
