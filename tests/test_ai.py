"""Pain points to goals: safety screen, the model call and its checks, and the keyword fallback."""

import json

import httpx
import pytest
from pydantic import SecretStr

from habitquest_backend import ai, llm
from habitquest_backend.ai import Candidate, keyword_goals, map_pain_point

CANDIDATES = [
    Candidate(
        "bedtime",
        "Calmer bedtimes",
        "Bed on time.",
        ["bed", "bedtime", "sleep"],
        [
            {"id": "bedtime", "title": "Get to bed early", "tier": 1},
            {"id": "brush-teeth", "title": "Brush teeth", "tier": 1},
            {"id": "story-time", "title": "Share a book", "tier": 1},
        ],
    ),
    Candidate(
        "meals",
        "Happier mealtimes",
        "Calm meals.",
        ["dinner", "eat", "picky"],
        [
            {"id": "dinner", "title": "Finish dinner on time", "tier": 1},
        ],
    ),
    Candidate(
        "screens",
        "Healthy screen time",
        "Less screens.",
        ["tablet", "screen"],
        [
            {"id": "screen", "title": "Reduce screen time", "tier": 1},
        ],
    ),
    Candidate(
        "homework",
        "Homework without battles",
        "Homework.",
        ["homework"],
        [
            {"id": "homework", "title": "Complete homework daily", "tier": 1},
        ],
    ),
]
BEDTIME = "Bedtime is a battle every night and he will not sleep"


@pytest.fixture
def remote(settings):
    return settings.model_copy(
        update={
            "ai_mode": "openai",
            "openai_base_url": "https://llm.example/v1/",
            "openai_model": "vendor/model",
            "openai_api_key": SecretStr("k"),
        }
    )


def serve(monkeypatch, handler):
    """Routes the model call to `handler` instead of the network; returns the captured requests."""
    seen = []

    def respond(request):
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        llm, "http_client", lambda settings: httpx.Client(transport=httpx.MockTransport(respond))
    )
    return seen


def chat(content):
    message = {"role": "assistant", "content": content}
    choice = {"index": 0, "finish_reason": "stop", "message": message}
    body = {"id": "x", "object": "chat.completion", "created": 0, "model": "m", "choices": [choice]}
    return lambda request: httpx.Response(200, json=body)


def test_fake_mode_maps_keywords_and_recommends_the_first_habits(settings):
    mapping = map_pain_point(settings, 8, BEDTIME + ", and he is a picky eater at dinner", CANDIDATES)
    assert mapping.mapped_by == "ai" and mapping.safety is None
    assert [goal for goal, _ in mapping.goals] == ["meals", "bedtime"]  # most keyword hits first
    assert dict(mapping.goals)["bedtime"] == "You mentioned bedtime, sleep."
    assert mapping.recommended == {"bedtime": ["bedtime", "brush-teeth"], "meals": ["dinner"]}


def test_nothing_recognisable_leaves_the_choice_to_the_parent(settings):
    mapping = map_pain_point(settings, 8, "Things are just hard lately", CANDIDATES)
    assert (mapping.mapped_by, mapping.goals, mapping.recommended) == ("manual", [], {})


@pytest.mark.parametrize(
    "text",
    [
        "She said she wants to die",
        "I think he is self-harming",
        "He keeps hurting himself when angry",
        "I worry she is being abused at school",
        "He is not safe at home with his uncle",
    ],
)
def test_risk_signs_return_guidance_instead_of_goals(settings, text):
    mapping = map_pain_point(settings, 9, text, CANDIDATES)
    assert mapping.safety == ai.SAFETY_GUIDANCE and mapping.goals == []


def test_the_model_answer_is_limited_to_the_catalog_it_was_shown(remote, monkeypatch):
    answer = {
        "goals": [
            {"goal_id": "flying", "reason": "Not in the list."},
            {
                "goal_id": "bedtime",
                "reason": " Evenings drag on. ",
                "recommended_habits": ["story-time", "nap", "story-time", "bedtime", "brush-teeth"],
            },
            {"goal_id": "bedtime", "reason": "Again."},
            {"goal_id": "screens", "reason": "Tablet time."},
            {"goal_id": "meals", "reason": "Dinner.", "recommended_habits": ["bedtime"]},
            {"goal_id": "homework", "reason": "Fourth goal."},
        ]
    }
    seen = serve(monkeypatch, chat("```json\n" + json.dumps(answer) + "\n```"))
    mapping = map_pain_point(remote, 7, BEDTIME, CANDIDATES)
    assert mapping.mapped_by == "ai"
    assert mapping.goals == [
        ("bedtime", "Evenings drag on."),
        ("screens", "Tablet time."),
        ("meals", "Dinner."),
    ]
    assert mapping.recommended == {"bedtime": ["story-time", "bedtime"], "screens": [], "meals": []}
    request = seen[0]
    assert str(request.url) == "https://llm.example/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer k"
    body = json.loads(request.content)
    assert (body["model"], body["temperature"], body["response_format"]) == (
        "vendor/model",
        0,
        {"type": "json_object"},
    )
    sent = json.loads(body["messages"][1]["content"])
    assert (sent["child_age"], sent["parent_wrote"]) == (7, BEDTIME)
    assert [g["id"] for g in sent["goals"]] == ["bedtime", "meals", "screens", "homework"]


def test_the_model_can_flag_risk(remote, monkeypatch):
    serve(monkeypatch, chat('{"at_risk": true, "goals": []}'))
    assert map_pain_point(remote, 7, "Something worrying happened", CANDIDATES).safety == ai.SAFETY_GUIDANCE


@pytest.mark.parametrize(
    "handler",
    [
        lambda request: httpx.Response(500, json={"error": "overloaded"}),
        chat("I think bedtime is the main goal."),  # not JSON
        chat('{"goals": "bedtime"}'),  # wrong shape
        chat('{"goals": [{"goal_id": "flying"}]}'),  # nothing usable
        lambda request: httpx.Response(200, json={"choices": []}),
    ],
)
def test_model_failures_fall_back_to_keywords(remote, monkeypatch, handler):
    serve(monkeypatch, handler)
    mapping = map_pain_point(remote, 7, BEDTIME, CANDIDATES)
    assert (mapping.mapped_by, [g for g, _ in mapping.goals], mapping.recommended) == (
        "keywords",
        ["bedtime"],
        {},
    )


def test_an_unreachable_model_falls_back_to_keywords(remote, monkeypatch):
    def down(request):
        raise httpx.ConnectTimeout("timed out")

    serve(monkeypatch, down)
    assert map_pain_point(remote, 7, BEDTIME, CANDIDATES).mapped_by == "keywords"


def test_keywords_match_whole_words_with_common_endings():
    found = dict(
        keyword_goals("Lately the tablets come out before homework; she is a picky eater", CANDIDATES)
    )
    assert list(found) == ["meals", "screens", "homework"]  # eater, tablets, homework; not "late" in lately
    assert keyword_goals("He is sleepy and bedridden", CANDIDATES) == []


def test_keywords_rank_by_hits_and_cap_at_three():
    text = "bed bedtime sleep, dinner eat, tablet, homework"
    assert [goal for goal, _ in keyword_goals(text, CANDIDATES)] == ["bedtime", "meals", "screens"]


def test_the_http_client_uses_the_configured_timeout(settings):
    with llm.http_client(settings.model_copy(update={"ai_timeout_seconds": 7})) as client:
        assert client.timeout.read == 7


def test_openai_mode_needs_its_settings(settings):
    from pydantic import ValidationError

    from habitquest_backend.config import Settings

    with pytest.raises(ValidationError, match="AI_MODE=openai requires"):
        Settings(ai_mode="openai", openai_model="m")


def test_a_rejected_request_is_logged_with_its_status_but_no_content(remote, monkeypatch, caplog):
    serve(monkeypatch, lambda request: httpx.Response(400, json={"error": "response_format not supported"}))
    map_pain_point(remote, 7, BEDTIME, CANDIDATES)
    assert "OpenAIInvalidRequestError 400" in caplog.text and BEDTIME not in caplog.text
