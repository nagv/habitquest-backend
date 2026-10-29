import json

from conftest import queued
from langchain_core.messages import AIMessage, ToolMessage

from habitquest_backend import planning, worker


def test_model_cannot_silently_drop_a_confirmed_goal(client, settings, monkeypatch):
    _, _, run = queued(client)
    original = planning.BoundedChat._fake

    def omit(self, messages):
        if self.role == "planner" and any(isinstance(m, ToolMessage) for m in messages):
            return AIMessage(
                content=json.dumps(
                    {
                        "option_ids": ["bedtime-prep"],
                        "rationale": "Only bedtime selected despite three confirmed goals.",
                    }
                )
            )
        return original(self, messages)

    monkeypatch.setattr(planning.BoundedChat, "_fake", omit)
    worker.run_once(settings)
    assert client.get(f"/v1/plan-runs/{run}").json()["status"] == "failed"
