"""The quest planner, weekly review and buddy lines: checks on the answers, names, and who can call them."""

import json

import httpx
import pytest
from conftest import OTHER, PARENT, token
from fastapi.testclient import TestClient

from habitquest_backend.insights import BUDDY_FALLBACK
from habitquest_backend.main import create_app

PLAN = {
    "summary": "Child A is doing well.",
    "tips": ["Child A could set an alarm."],
    "changes": [
        {
            "child": "Child A",
            "habit_id": "bedtime",
            "action": "retime",
            "cue_time": "19:45",
            "reason": "Child A is tired.",
        },
        {"child": "Child A", "habit_id": "bedtime", "action": "pause", "reason": "A duplicate."},
        {"child": "Child A", "habit_id": "homework", "action": "add", "reason": ""},
        {"child": "Child A", "habit_id": "flying", "action": "add", "reason": "Not in the catalog."},
        {"child": "Child A", "habit_id": "dinner", "action": "pause", "reason": "Not in the plan."},
        {"child": "Child Z", "habit_id": "bedtime", "action": "pause", "reason": "No such child."},
    ],
}
REVIEW = {
    "headline": "A good week for Child A.",
    "children": [
        {"child": "Child A", "wins": "Child A went to bed on time.", "watch": "Nothing to worry about"}
    ],
    "patterns": ["Child A did better this week."],
    "changes": [],
}


@pytest.fixture
def family(client, db):
    for user in (PARENT, OTHER):
        db.execute("INSERT INTO hq.users(id,name) VALUES(%s,'Test')", (user,))
    body = {"name": "Rivera", "pin": "1234", "display_name": "Sam", "role": "mother"}
    return client.post("/v1/families", json=body).json()["id"]


@pytest.fixture
def alex(client, family):
    child = client.post(f"/v1/families/{family}/children", json={"name": "Alex", "age": 8}).json()["id"]
    client.put(f"/v1/children/{child}/plan", json={"goals": ["bedtime"], "habits": [{"habit_id": "bedtime"}]})
    return child


@pytest.fixture
def live(settings, remote, family, alex):
    with TestClient(create_app(remote)) as c:
        c.headers["Authorization"] = "Bearer " + token(settings)
        yield c


def kid_headers(client, child):
    return {
        "Authorization": "Bearer " + client.post(f"/v1/children/{child}/kid-session").json()["access_token"]
    }


def test_the_planner_saves_only_changes_that_can_apply(live, family, llm_reply):
    llm_reply(json.dumps(PLAN))
    response = live.post(f"/v1/families/{family}/plan-suggestions")
    assert response.status_code == 200, response.text
    assert response.json()["result"]["summary"] == "Alex is doing well."
    assert response.json()["result"]["tips"] == ["Alex could set an alarm."]
    cards = live.get(f"/v1/families/{family}/suggestions").json()
    assert [(c["kind"], c["habit_id"], c["cue_time"], c["reason"]) for c in cards] == [
        ("retime", "bedtime", "19:45", "Alex is tired."),
        ("add", "homework", None, "This could help."),
    ]
    assert cards[0]["source"] == "planner"
    sent = json.dumps(llm_reply.seen)
    assert "Alex" not in sent and "Child A" in sent


def test_a_retime_without_a_valid_time_is_dropped(live, family, llm_reply):
    change = {
        "child": "Child A",
        "habit_id": "bedtime",
        "action": "retime",
        "cue_time": "late",
        "reason": "x",
    }
    llm_reply(json.dumps({"summary": "Fine.", "changes": [change]}))
    live.post(f"/v1/families/{family}/plan-suggestions")
    assert live.get(f"/v1/families/{family}/suggestions").json() == []


def test_the_review_uses_this_week_and_last(live, family, alex, db, llm_reply):
    habit = db.execute("SELECT id FROM hq.child_habits WHERE child_id=%s", (alex,)).fetchone()[0]
    for offset in (1, 9):
        db.execute(
            "INSERT INTO hq.completions(child_habit_id,day,status,decided_by,decided_at)"
            " VALUES(%s,hq.child_today(%s)-%s,'approved',%s,now())",
            (habit, alex, offset, PARENT),
        )
    llm_reply(json.dumps(REVIEW))
    body = live.post(f"/v1/families/{family}/reviews").json()
    assert body["kind"] == "review" and body["result"]["headline"] == "A good week for Alex."
    assert body["result"]["children"][0]["wins"] == "Alex went to bed on time."
    assert '\\"this_week\\": 1, \\"week_before\\": 1' in json.dumps(llm_reply.seen)


def test_reports_return_the_newest_of_each(live, family, llm_reply):
    assert live.get(f"/v1/families/{family}/reports").json() == {"plan": None, "review": None}
    llm_reply(json.dumps(REVIEW))
    live.post(f"/v1/families/{family}/reviews")
    live.post(f"/v1/families/{family}/reviews")
    found = live.get(f"/v1/families/{family}/reports").json()
    assert found["plan"] is None and found["review"]["kind"] == "review"


def test_an_unavailable_model_saves_nothing(live, family, llm_reply, db):
    llm_reply(httpx.Response(503, json={"error": "down"}))
    assert live.post(f"/v1/families/{family}/plan-suggestions").status_code == 503
    assert db.execute("SELECT count(*) FROM hq.ai_reports").fetchone()[0] == 0


def test_fake_mode_writes_plain_reports(client, family, alex):
    assert client.post(f"/v1/families/{family}/plan-suggestions").json()["result"]["tips"] == []
    body = client.post(f"/v1/families/{family}/reviews").json()
    assert body["result"]["children"][0]["wins"] == "0 quests approved this week."


def test_a_family_needs_a_child_first(client, family):
    assert client.post(f"/v1/families/{family}/reviews").status_code == 409


def test_outsiders_and_kids_cannot_read_reports(client, settings, family, alex):
    headers = {"Authorization": "Bearer " + token(settings, OTHER)}
    assert client.post(f"/v1/families/{family}/reviews", headers=headers).status_code == 404
    assert client.get(f"/v1/families/{family}/reports", headers=headers).status_code == 404
    assert client.get(f"/v1/families/{family}/reports", headers=kid_headers(client, alex)).status_code == 401


def test_fake_mode_gives_the_built_in_line(client, alex):
    headers = kid_headers(client, alex)
    response = client.post("/v1/kid/buddy-line", json={"event": "greet"}, headers=headers)
    assert response.json() == {"line": BUDDY_FALLBACK["greet"]}


def test_a_safe_line_reaches_the_child_without_their_name(live, alex, llm_reply):
    llm_reply('"Great  job on your\nquests!"', '{"safe": true, "category": "none", "risk": false}')
    response = live.post("/v1/kid/buddy-line", json={"event": "submitted"}, headers=kid_headers(live, alex))
    assert response.json() == {"line": "Great job on your quests!"}
    assert llm_reply.seen[0]["model"] == "vendor/small" and "Alex" not in json.dumps(llm_reply.seen)


@pytest.mark.parametrize(
    "replies",
    [
        ("",),
        ("word " * 40,),
        ("You are so dumb.",),
        ("A nice line.", '{"safe": false, "category": "unkind", "risk": false}'),
        (httpx.Response(503, json={"error": "down"}),),
    ],
)
def test_an_unusable_line_is_replaced(live, alex, llm_reply, replies):
    llm_reply(*replies)
    response = live.post("/v1/kid/buddy-line", json={"event": "celebrate"}, headers=kid_headers(live, alex))
    assert response.json() == {"line": BUDDY_FALLBACK["celebrate"]}


def test_the_daily_limit_gives_the_built_in_line(settings, remote, alex, llm_reply):
    limited = remote.model_copy(update={"ai_daily_limit": 1})
    llm_reply("A nice line.", '{"safe": true, "category": "none", "risk": false}')
    with TestClient(create_app(limited)) as c:
        c.headers["Authorization"] = "Bearer " + token(settings)
        headers = kid_headers(c, alex)
        assert (
            c.post("/v1/kid/buddy-line", json={"event": "greet"}, headers=headers).json()["line"]
            == "A nice line."
        )
        second = c.post("/v1/kid/buddy-line", json={"event": "greet"}, headers=headers).json()
    assert second["line"] == BUDDY_FALLBACK["greet"]
