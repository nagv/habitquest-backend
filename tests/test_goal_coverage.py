import json

from conftest import queued
from langchain_core.messages import AIMessage, ToolMessage

from habitquest_backend import planning, worker


def test_model_cannot_silently_drop_a_template_goal(client, settings, monkeypatch):
    _, _, run = queued(client)
    original = planning.BoundedChat._fake

    def omit(self, messages):
        if self.role == "planner" and any(isinstance(m, ToolMessage) for m in messages):
            return AIMessage(
                content=json.dumps(
                    {
                        "picks": [{"habit_id": "bedtime-prep", "level": "standard"}],
                        "rationale": "Only bedtime despite three coverable goals.",
                        "order_explanation": "Bedtime only.",
                    }
                )
            )
        return original(self, messages)

    monkeypatch.setattr(planning.BoundedChat, "_fake", omit)
    worker.run_once(settings)
    data = client.get(f"/v1/proposals/{run}").json()["data"]
    assert data["fallback"]
    assert [h["habit_id"] for h in data["active"]] == ["bedtime-prep", "clear-plate", "join-meal"]
