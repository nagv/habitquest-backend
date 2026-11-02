"""Coach traces: what would reach LangSmith, and that a trace can never affect a reply."""

import json
import os

import httpx
import pytest
from langchain_core.messages import HumanMessage
from pydantic import SecretStr
from pydantic_settings import SettingsError

from habitquest_backend import tracing
from habitquest_backend.ai import Candidate
from habitquest_backend.coach import age_band, run_turn
from habitquest_backend.coach_tools import Snapshot
from habitquest_backend.config import Settings
from habitquest_backend.tracing import keeps_text, redact, scrub, send_trace, start_trace

HABITS = [{"id": "bedtime", "title": "Get to bed", "tier": 1}]
GOALS = [Candidate("bedtime", "Calmer bedtimes", "Bed on time.", ["bedtime"], HABITS)]
NOT_PRIVATE = (
    "Maya ignores Raj at bedtime, write to raj.v@example.com or call +44 7700 900123, 12 Oak Tree Road"
)


class Sender:
    def __init__(self):
        self.runs = []

    def create_run(self, name, inputs, run_type, **values):
        self.runs.append({"name": name, "inputs": inputs, "run_type": run_type, **values})

    @property
    def text(self):
        return json.dumps(self.runs, default=str)


@pytest.fixture
def sender(monkeypatch):
    found = Sender()
    monkeypatch.setattr(tracing, "client", lambda url, key: found)
    return found


@pytest.fixture
def traced(remote):
    return remote.model_copy(
        update={"trace_coach": True, "langsmith_api_key": SecretStr("key"), "trace_sample": 0.0}
    )


def snapshot():
    return Snapshot(7, GOALS, {}, {})


def call(name, **args):
    function = {"name": name, "arguments": json.dumps(args)}
    return {"content": "", "tool_calls": [{"id": f"c-{name}", "type": "function", "function": function}]}


def test_automatic_tracing_is_off_whatever_the_environment_says():
    assert os.environ["LANGSMITH_TRACING"] == "false" and os.environ["LANGCHAIN_TRACING_V2"] == "false"


def test_redaction_removes_known_names_and_shaped_details():
    clean = redact(NOT_PRIVATE, ["Maya", "Raj Vorugan"])
    assert clean == "[name] ignores [name] at bedtime, write to [email] or call [phone], [address]"
    assert (
        redact("See https://example.com/a and www.example.com, aged 7", [])
        == "See [link] and [link], aged [age]"
    )


def test_short_times_and_plain_numbers_are_not_mistaken_for_phones():
    assert redact("Bed at 20:30, 3 times in 14 days", []) == "Bed at 20:30, 3 times in 14 days"


def test_scrubbing_reaches_nested_text_and_hides_unknown_types():
    value = {"a": ["Maya", 3, None, True], "b": ("Maya",), "c": object()}
    assert scrub(value, ["Maya"], True) == {"a": ["[name]", 3, None, True], "b": ["[name]"], "c": "[hidden]"}
    assert scrub({"a": "Maya"}, ["Maya"], False) == {"a": "[4 characters]"}


def test_messages_stay_readable_with_personal_details_removed():
    clean = scrub({"messages": [HumanMessage(content="Maya ignores me")]}, ["Maya"], True)
    assert clean["messages"][0]["content"] == "[name] ignores me" and clean["messages"][0]["type"] == "human"
    hidden = scrub([HumanMessage(content="Maya")], ["Maya"], False)
    assert hidden[0]["content"] == "[4 characters]"


@pytest.mark.parametrize(("age", "band"), [(3, "3-5"), (5, "3-5"), (6, "6-8"), (8, "6-8"), (9, "9-13")])
def test_ages_become_bands(age, band):
    assert age_band(age) == band


def test_sampling_keeps_wanted_turns_and_the_chosen_share(remote):
    none, every = (
        remote.model_copy(update={"trace_sample": 0.0}),
        remote.model_copy(update={"trace_sample": 1.0}),
    )
    assert keeps_text(none, wanted=True) and keeps_text(every, wanted=False)
    assert not keeps_text(none, wanted=False)


def test_tracing_needs_a_key_and_is_off_by_default(remote):
    assert start_trace(remote) is None
    send_trace(remote, None, [], True, {})
    with pytest.raises((ValueError, SettingsError)):
        Settings(trace_coach=True, langsmith_api_key=None, _env_file=None)


def test_an_ordinary_turn_sends_structure_without_any_text(traced, sender, llm_reply):
    llm_reply("When does it happen?")
    run_turn(traced, snapshot(), [], NOT_PRIVATE, ["Maya"], ["Raj Vorugan"])
    assert sender.runs and "Oak Tree" not in sender.text and "bedtime" not in sender.text
    root = next(r for r in sender.runs if r["parent_run_id"] is None)
    assert root["extra"]["metadata"]["outcome"] == "question"
    assert root["extra"]["metadata"]["age_band"] == "6-8" and root["project_name"] == "habitquest"


def test_a_no_match_turn_keeps_redacted_text_and_no_personal_details(traced, sender, llm_reply):
    llm_reply(call("research", problem="lying"), "Tell Raj to stay calm.")
    reply = run_turn(traced, snapshot(), [], NOT_PRIVATE, ["Maya"], ["Raj Vorugan"])
    assert reply.no_habit_match and "stay calm" in sender.text
    for private in ["Maya", "Raj", "raj.v", "7700", "Oak Tree"]:
        assert private not in sender.text
    root = next(r for r in sender.runs if r["parent_run_id"] is None)
    assert root["extra"]["metadata"]["outcome"] == "no_habit_match"


def test_cards_appear_in_the_outcome_of_a_sampled_out_turn(traced, sender, llm_reply):
    llm_reply(
        call("recommend_habits", quests=[{"habit_id": "bedtime", "reason": "It helps."}]),
        "Here is one.",
        '{"poor_fit": []}',
    )
    run_turn(traced, snapshot(), [], "Stalling at bedtime", ["Maya"])
    metadata = next(r for r in sender.runs if r["parent_run_id"] is None)["extra"]["metadata"]
    assert (metadata["outcome"], metadata["habits"], metadata["retried"]) == ("cards", ["bedtime"], False)


def test_a_failed_turn_is_traced_and_still_falls_back(traced, sender, llm_reply):
    llm_reply(httpx.Response(401, json={"error": "bad key"}))
    reply = run_turn(traced, snapshot(), [], "Stalling at bedtime", ["Maya"])
    assert reply.mapped_by == "keywords"
    root = next(r for r in sender.runs if r["parent_run_id"] is None)
    assert root["extra"]["metadata"]["outcome"] == "failed" and root["error"]


def test_a_tracing_failure_never_reaches_the_reply(traced, monkeypatch, llm_reply, caplog):
    def broken(url, key):
        raise ConnectionError("Maya")

    monkeypatch.setattr(tracing, "client", broken)
    llm_reply("When does it happen?")
    assert run_turn(traced, snapshot(), [], "Evenings are hard", ["Maya"]).text == "When does it happen?"
    assert "ConnectionError" in caplog.text and "Maya" not in caplog.text


def test_the_client_is_built_once(monkeypatch):
    monkeypatch.setattr(tracing, "Client", lambda api_url, api_key: object())
    tracing.client.cache_clear()
    assert tracing.client("http://x", "key") is tracing.client("http://x", "key")
    tracing.client.cache_clear()
