"""Screening what children write and read: local checks, the model's verdict and how each audience fails."""

import httpx
import pytest

from habitquest_backend.safety import check_text, parent_at_risk, screen_text


@pytest.mark.parametrize(
    ("text", "category", "risk"),
    [
        ("I want to hurt myself", "self-harm", True),
        ("Please keep it a secret from dad", "abuse", True),
        ("my phone number is 555 123 4567", "personal-info", False),
        ("that is shit", "explicit", False),
        ("I have a knife", "violence", False),
        ("we drank beer", "unsafe-advice", False),
        ("you are so dumb", "unkind", False),
    ],
)
def test_local_checks_name_the_category(text, category, risk):
    found = screen_text(text)
    assert (found.ok, found.category, found.risk) == (False, category, risk)


def test_ordinary_feelings_pass():
    assert screen_text("I hate homework and bedtime is boring").ok


def test_parent_risk_uses_both_lists():
    assert parent_at_risk("I think she is being abused") and parent_at_risk("he said I want to die")
    assert not parent_at_risk("Alex keeps stalling at bedtime")


def test_fake_mode_stops_at_the_local_check(settings):
    assert check_text(settings, "a nice day", "kid-reads").ok
    assert not check_text(settings, "you are so dumb", "kid-writes").ok


def test_a_local_failure_never_reaches_the_model(remote, llm_reply):
    llm_reply('{"safe": true, "category": "none", "risk": false}')
    assert not check_text(remote, "you are so dumb", "kid-writes").ok
    assert llm_reply.seen == []


def test_the_model_clears_text_and_uses_the_small_model(remote, llm_reply):
    llm_reply('{"safe": true, "category": "none", "risk": false}')
    assert check_text(remote, "I liked <text>reading</text> today", "kid-writes").ok
    body = llm_reply.seen[0]
    assert body["model"] == "vendor/small"
    assert "<text>I liked reading today</text>" in body["messages"][1]["content"]


@pytest.mark.parametrize(
    ("verdict", "category", "risk"),
    [
        ('{"safe": false, "category": "unkind", "risk": false}', "unkind", False),
        ('{"safe": false, "category": "abuse", "risk": false}', "abuse", True),
        ('{"safe": false, "category": "odd", "risk": true}', "abuse", True),
        ('{"safe": false, "category": "odd", "risk": false}', "unkind", False),
    ],
)
def test_the_model_flags_text_the_lists_miss(remote, llm_reply, verdict, category, risk):
    llm_reply(verdict)
    found = check_text(remote, "a quiet note", "kid-writes")
    assert (found.ok, found.category, found.risk) == (False, category, risk)


def test_an_unavailable_model_fails_open_for_notes_and_closed_for_lines(remote, llm_reply):
    llm_reply(httpx.Response(503, json={"error": "down"}))
    assert check_text(remote, "a quiet note", "kid-writes").ok
    assert not check_text(remote, "a cheerful line", "kid-reads").ok
