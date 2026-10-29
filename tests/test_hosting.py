import threading
import time

import pytest
from conftest import queued, token
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import SecretStr, ValidationError

from habitquest_backend import planning, worker
from habitquest_backend.config import Settings
from habitquest_backend.main import create_app


def test_google_adapter_contract_without_calling_google(settings, monkeypatch):
    captured = {}

    class Transport:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def bind_tools(self, tools):
            captured["tools"] = tools
            return self

        def invoke(self, messages):
            return AIMessage(content='{"accepted":true,"reason":"ok"}')

    monkeypatch.setattr(planning, "ChatGoogleGenerativeAI", Transport)
    google = settings.model_copy(
        update={"model_mode": "google", "google_model": "gemini-test", "google_api_key": SecretStr("k")}
    )
    model = planning.BoundedChat(
        role="reviewer", settings=google, context={}, charge=lambda *a: None, started=time.monotonic()
    )
    assert "accepted" in model.invoke([HumanMessage(content="review")]).content
    assert model.bind_tools([{"name": "search_catalog"}]).invoke([HumanMessage(content="review")]).content
    assert (captured["model"], captured["google_api_key"], captured["temperature"]) == ("gemini-test", "k", 0)
    assert captured["timeout"] > 0 and captured["tools"] == [{"name": "search_catalog"}]


def test_google_mode_requires_model_and_key():
    with pytest.raises(ValidationError, match="GOOGLE_MODEL and GOOGLE_API_KEY"):
        Settings(model_mode="google", google_model="gemini-test")
    assert (
        Settings(model_mode="google", google_model="gemini-test", google_api_key="k").model_mode == "google"
    )


def test_serve_survives_a_failed_pass_and_stops(settings, monkeypatch):
    stop = threading.Event()
    results = iter([True, RuntimeError("database restarted"), False])

    def run_once(_):
        result = next(results)
        if isinstance(result, Exception):
            raise result
        if result is False:
            stop.set()
        return result

    monkeypatch.setattr(worker, "run_once", run_once)
    worker.serve(settings, stop)
    assert stop.is_set()


def test_worker_runs_inside_the_api_when_enabled(settings, db):
    with TestClient(create_app(settings.model_copy(update={"run_worker_in_api": True}))) as client:
        client.headers["Authorization"] = "Bearer " + token(settings)
        _, _, run = queued(client)
        deadline = time.monotonic() + 60
        while client.get(f"/v1/plan-runs/{run}").json()["status"] != "ready":
            assert time.monotonic() < deadline, "the in-process worker did not finish the run"
            time.sleep(0.5)
    assert not any(t.name == "plan-worker" for t in threading.enumerate())


def test_openai_compatible_adapter_contract_without_calling_the_provider(settings, monkeypatch):
    captured = {}

    class Transport:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def bind_tools(self, tools):
            captured["tools"] = tools
            return self

        def invoke(self, messages):
            return AIMessage(content='{"accepted":true,"reason":"ok"}')

    monkeypatch.setattr(planning, "ChatOpenAI", Transport)
    nebius = settings.model_copy(
        update={
            "model_mode": "openai",
            "openai_base_url": "https://llm.example/v1/",
            "openai_model": "vendor/model-instruct",
            "openai_api_key": SecretStr("k"),
        }
    )
    model = planning.BoundedChat(
        role="reviewer", settings=nebius, context={}, charge=lambda *a: None, started=time.monotonic()
    )
    assert "accepted" in model.invoke([HumanMessage(content="review")]).content
    assert model.bind_tools([{"name": "search_catalog"}]).invoke([HumanMessage(content="review")]).content
    assert (captured["model"], captured["base_url"], captured["temperature"]) == (
        "vendor/model-instruct",
        "https://llm.example/v1/",
        0,
    )
    assert captured["api_key"].get_secret_value() == "k"
    assert captured["timeout"] > 0 and captured["max_completion_tokens"] == 1024


def test_openai_mode_requires_endpoint_model_and_key():
    with pytest.raises(ValidationError, match="OPENAI_BASE_URL, OPENAI_MODEL and OPENAI_API_KEY"):
        Settings(model_mode="openai", openai_model="m", openai_api_key="k")
    configured = Settings(
        model_mode="openai", openai_base_url="https://llm.example/v1/", openai_model="m", openai_api_key="k"
    )
    assert configured.model_mode == "openai"


def test_every_endpoint_is_documented(settings):
    """/docs shows a purpose for every operation, grouped under a described tag."""
    spec = create_app(settings).openapi()
    tags = {tag["name"] for tag in spec["tags"]}
    operations = [(method, path, op) for path, ops in spec["paths"].items() for method, op in ops.items()]
    assert len(operations) >= 36
    undocumented = [
        (method, path)
        for method, path, op in operations
        if not op.get("summary") or len(op.get("description", "")) < 40 or not set(op.get("tags", [])) <= tags
    ]
    assert undocumented == []
