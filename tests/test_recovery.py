import json
import time

import pytest
from conftest import queued
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from habitquest_backend import planning, worker


def test_empty_queue_and_exhausted_lease(settings, db, client):
    assert not worker.run_once(settings)
    _, _, run = queued(client)
    db.execute("UPDATE hq.jobs SET status='running',attempts=3,lease_until=now()-interval '1 second'")
    assert not worker.run_once(settings)
    assert client.get(f"/v1/plan-runs/{run}").json()["status"] == "failed"


def test_expired_lease_reclaims_once_and_fences_previous_owner(client, settings, db):
    _, _, run = queued(client)
    first = worker.claim(settings)
    assert worker.claim(settings) is None
    assert worker.heartbeat(settings, first)
    db.execute("UPDATE hq.jobs SET lease_until=now()-interval '1 second'")
    second = worker.claim(settings)
    assert second["attempts"] == 2 and second["lease_token"] != first["lease_token"]
    assert not worker.heartbeat(settings, first)
    db.execute("UPDATE hq.jobs SET lease_until=now()-interval '1 second'")
    assert worker.run_once(settings)
    assert client.get(f"/v1/plan-runs/{run}").json()["status"] == "ready"


def test_cancelled_jobs_are_not_claimed(client, settings):
    _, _, run = queued(client)
    assert client.post(f"/v1/plan-runs/{run}/cancel").status_code == 200
    assert not worker.run_once(settings)
    assert client.post(f"/v1/plan-runs/{run}/cancel").status_code == 404


@pytest.mark.parametrize("budget", [{"max_model_calls": 1}, {"max_run_tokens": 1024}])
def test_run_budget_stops_unreviewed_publication(client, settings, db, budget):
    _, _, run = queued(client)
    assert worker.run_once(settings.model_copy(update=budget))
    assert client.get(f"/v1/plan-runs/{run}").json()["status"] == "failed"
    assert db.execute("SELECT count(*) FROM hq.proposals").fetchone()[0] == 0


def test_provider_failure_retries_then_stops(client, settings, db, monkeypatch):
    _, _, run = queued(client)
    monkeypatch.setattr(
        worker, "generate", lambda *a: (_ for _ in ()).throw(ConnectionError("provider unavailable"))
    )
    for attempt in range(3):
        db.execute("UPDATE hq.jobs SET available_at=now()")
        assert worker.run_once(settings)
        result = client.get(f"/v1/plan-runs/{run}").json()
        assert result["attempts"] == attempt + 1
        assert result["status"] == ("failed" if attempt == 2 else "queued")


def test_changed_onboarding_blocks_worker_before_model_call(client, settings, db):
    _, _, run = queued(client)
    db.execute("UPDATE hq.intakes SET version=version+1")
    assert worker.run_once(settings)
    result = client.get(f"/v1/plan-runs/{run}").json()
    assert result["status"] == "failed" and result["model_calls"] == 0


def test_state_changes_during_generation_do_not_publish(client, settings, db, monkeypatch):
    _, _, run = queued(client)
    original = worker.generate

    def change(*args):
        result = original(*args)
        db.execute("UPDATE hq.intakes SET version=version+1")
        return result

    monkeypatch.setattr(worker, "generate", change)
    assert worker.run_once(settings)
    assert client.get(f"/v1/plan-runs/{run}").json()["status"] == "failed"
    assert db.execute("SELECT count(*) FROM hq.proposals").fetchone()[0] == 0


def test_cancellation_during_generation_fences_model_and_publication(client, settings, monkeypatch):
    _, _, run = queued(client)
    original = worker.generate

    def cancel(*args):
        assert client.post(f"/v1/plan-runs/{run}/cancel").status_code == 200
        return original(*args)

    monkeypatch.setattr(worker, "generate", cancel)
    assert worker.run_once(settings)
    assert client.get(f"/v1/plan-runs/{run}").json()["status"] == "cancelled"


@pytest.mark.parametrize("unsafe_concerns", ["punish the child", "starve to teach a lesson"])
def test_harmful_goals_fail_closed(client, settings, db, unsafe_concerns):
    _, _, run = queued(client)
    db.execute("UPDATE hq.intakes SET data=jsonb_set(data,'{concerns}',%s)", (json.dumps(unsafe_concerns),))
    assert worker.run_once(settings)
    assert client.get(f"/v1/plan-runs/{run}").json()["status"] == "failed"


@pytest.mark.parametrize(
    "picks",
    [
        [{"habit_id": "invented", "level": "standard"}],
        [{"habit_id": "bedtime-prep", "level": "standard"}] * 2,
    ],
)
def test_invalid_picks_fall_back_to_the_reviewed_template(client, settings, monkeypatch, picks):
    _, _, run = queued(client)
    original = planning.BoundedChat._fake

    def corrupt(self, messages):
        if self.role == "planner" and any(isinstance(m, ToolMessage) for m in messages):
            return AIMessage(
                content=json.dumps(
                    {
                        "picks": picks,
                        "rationale": "Plausible but invalid",
                        "order_explanation": "No particular order",
                    }
                )
            )
        return original(self, messages)

    monkeypatch.setattr(planning.BoundedChat, "_fake", corrupt)
    assert worker.run_once(settings)
    data = client.get(f"/v1/proposals/{run}").json()["data"]
    assert data["fallback"] and data["review"] is None
    assert [h["habit_id"] for h in data["active"]] == ["bedtime-prep", "clear-plate", "join-meal"]


@pytest.mark.parametrize("reject_count", [1, 2])
def test_final_review_is_required_after_revision(client, settings, db, monkeypatch, reject_count):
    _, _, run = queued(client)
    original = planning.BoundedChat._fake
    calls = []

    def review(self, messages):
        if self.role == "reviewer":
            calls.append(1)
            return AIMessage(
                content=json.dumps({"accepted": len(calls) > reject_count, "reason": "Review outcome"})
            )
        return original(self, messages)

    monkeypatch.setattr(planning.BoundedChat, "_fake", review)
    assert worker.run_once(settings)
    result = client.get(f"/v1/plan-runs/{run}").json()
    assert len(calls) == 2
    assert result["status"] == "ready"
    assert client.get(f"/v1/proposals/{run}").json()["data"]["fallback"] is (reject_count == 2)


def test_time_budget_and_nontext_json(settings):
    model = planning.BoundedChat(
        role="reviewer",
        settings=settings,
        context={},
        charge=lambda *a: None,
        started=time.monotonic() - 1000,
    )
    with pytest.raises(planning.BudgetExceeded):
        model.invoke([HumanMessage(content="test")])
    with pytest.raises(ValueError):
        planning.parse_json([])
    assert planning.parse_json('```json\n{"ok":true}\n```') == {"ok": True}


def test_ollama_adapter_contract_without_downloading_a_model(settings, monkeypatch):
    captured = {}

    class LocalTransport:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def bind_tools(self, tools):
            captured["tools"] = tools
            return self

        def invoke(self, messages):
            return AIMessage(content='{"accepted":true,"reason":"ok"}')

    monkeypatch.setattr(planning, "ChatOllama", LocalTransport)
    model = planning.BoundedChat(
        role="reviewer",
        settings=settings.model_copy(update={"model_mode": "ollama"}),
        context={},
        charge=lambda *a: None,
        started=time.monotonic(),
    )
    assert "accepted" in model.invoke([HumanMessage(content="review")]).content
    assert model.bind_tools([{"name": "search_catalog"}]).invoke([HumanMessage(content="review")]).content
    assert captured["base_url"] == "http://127.0.0.1:11434"
    assert captured["client_kwargs"]["timeout"] > 0
