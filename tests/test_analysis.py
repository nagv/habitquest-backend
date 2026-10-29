from conftest import onboard

from habitquest_backend.worker import run_once


def test_free_text_concerns_produce_goal_suggestions_before_parent_confirmation(client, settings):
    _, session = onboard(client, goals=[], bedtime=None, morning=None)
    response = client.post(
        f"/v1/onboarding/{session['id']}/analyze-runs", headers={"Idempotency-Key": "analyze"}
    )
    assert response.status_code == 202, response.text
    assert run_once(settings)
    run = client.get("/v1/plan-runs/" + response.json()["run_id"]).json()
    assert run["status"] == "ready"
    assert run["analysis"]["goals"] == ["bedtime", "morning", "responsibility"]
    assert len(run["analysis"]["questions"]) == 2
    assert (
        client.post(
            f"/v1/onboarding/{session['id']}/plan-runs", headers={"Idempotency-Key": "plan"}
        ).status_code
        == 409
    )
