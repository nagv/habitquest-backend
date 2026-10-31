"""The habit coach: the agent graph, its tools and checks, the conversation, and accepting suggestions."""

import json
import threading

import httpx
import pytest
from conftest import OTHER, PARENT, token
from fastapi.testclient import TestClient

from habitquest_backend import coach
from habitquest_backend.ai import SAFETY_GUIDANCE, Candidate
from habitquest_backend.coach import FALLBACK_MATCH, FALLBACK_NONE, fallback, run_turn, swap_names
from habitquest_backend.coach_tools import Snapshot, Turn, build_tools
from habitquest_backend.guidance import search_guidance
from habitquest_backend.main import create_app

HABITS = [
    {"id": "bedtime", "title": "Get to bed early", "tier": 1},
    {"id": "story-time", "title": "Share a book", "tier": 1},
    {"id": "wash-hands", "title": "Wash hands", "tier": 1},
]
GOALS = [Candidate("bedtime", "Calmer bedtimes", "Bed on time.", ["bedtime", "sleep"], HABITS)]
BEDTIME = "Alex keeps stalling at bedtime"
PLAN = {"homework": {"title": "Homework", "cue_time": "16:00", "status": "active"}}
CHILD_HABIT = "44444444-4444-4444-4444-444444444444"
REF = search_guidance("stalling at bedtime", 8)[0].ref


def snapshot(**overrides):
    values = {"age": 8, "goals": GOALS, "plan": PLAN, "approved_days": {"homework": 3}}
    return Snapshot(**(values | overrides))


def call(name, **args):
    function = {"name": name, "arguments": json.dumps(args)}
    return {"content": "", "tool_calls": [{"id": f"c-{name}", "type": "function", "function": function}]}


def tools_of(snap, turn):
    return {t.name: t for t in build_tools(snap, turn)}


def pick(habit, reason="It helps."):
    return {"habit_id": habit, "reason": reason}


# ── Tools ─────────────────────────────────────────────────────────────────────


def test_research_leaves_out_the_plan_and_what_was_shown():
    turn = Turn()
    plan = {"bedtime": {"title": "Bed", "cue_time": "20:00", "status": "active"}}
    snap = snapshot(shown={"story-time"}, plan=plan)
    found = json.loads(tools_of(snap, turn)["research"].invoke({"problem": "stalling at bedtime"}))
    assert [h["habit_id"] for h in found["goals"][0]["habits"]] == ["wash-hands"]
    assert found["sources"] and set(turn.sources) == {s["ref"] for s in found["sources"]}


def test_research_says_when_it_found_no_guidance():
    found = json.loads(tools_of(snapshot(), Turn())["research"].invoke({"problem": "xylophone quantum"}))
    assert found["sources"] == [] and "note" in found


def test_research_drops_goals_with_nothing_left():
    snap = snapshot(shown={h["id"] for h in HABITS})
    assert json.loads(tools_of(snap, Turn())["research"].invoke({"problem": "bedtime"}))["goals"] == []


def test_progress_reports_the_plan():
    found = json.loads(tools_of(snapshot(), Turn())["get_progress"].invoke({}))
    assert found["plan"][0]["habit_id"] == "homework" and found["plan"][0]["approved_days"] == 3


def test_recommendations_refuse_unknown_shown_and_planned_habits():
    turn = Turn()
    goals = [Candidate("g", "G", "D", [], [*HABITS, {"id": "homework", "title": "Homework", "tier": 1}])]
    recommend = tools_of(snapshot(goals=goals, shown={"story-time"}), turn)["recommend_habits"]
    result = recommend.invoke({"quests": [pick("story-time"), pick("flying"), pick("homework")]})
    assert result.startswith("Error") and turn.recommendations == {}
    result = recommend.invoke(
        {"quests": [pick("bedtime", " Early nights. "), pick("wash-hands", " "), pick("flying")]}
    )
    assert "Skipped: flying" in result
    assert turn.recommendations == {"bedtime": "Early nights.", "wash-hands": "This could help."}


def test_recommending_without_errors_mentions_nothing_skipped():
    result = tools_of(snapshot(), Turn())["recommend_habits"].invoke({"quests": [pick("bedtime")]})
    assert "Skipped" not in result


def test_plan_changes_are_checked_and_replace_earlier_ones():
    turn = Turn()
    propose = tools_of(snapshot(), turn)["propose_plan_change"]
    assert propose.invoke({"habit_id": "bedtime", "action": "pause", "reason": "x"}).startswith("Error")
    assert propose.invoke({"habit_id": "homework", "action": "retime", "reason": "x"}).startswith("Error")
    propose.invoke({"habit_id": "homework", "action": "pause", "reason": "Rest.", "cue_time": "16:30"})
    propose.invoke({"habit_id": "homework", "action": "retime", "reason": "Later.", "cue_time": "16:30"})
    assert [(c.kind, c.cue_time) for c in turn.changes] == [("retime", "16:30")]


# ── One turn ──────────────────────────────────────────────────────────────────


def test_names_are_swapped_as_whole_words():
    assert swap_names("alex and Alexa", {"Alex": "Child A"}) == "Child A and Alexa"


def test_risky_text_gets_guidance_without_any_model_call(remote, llm_reply):
    llm_reply("never")
    reply = run_turn(remote, snapshot(), [], "He said he wants to die", ["Alex"])
    assert reply.safety == SAFETY_GUIDANCE and llm_reply.seen == []


def test_fake_mode_answers_from_keywords(settings):
    reply = run_turn(settings, snapshot(), [], BEDTIME, ["Alex"])
    assert (reply.text, reply.mapped_by) == (FALLBACK_MATCH, "keywords")
    assert list(reply.turn.recommendations) == ["bedtime"]


def test_fallback_skips_habits_already_taken():
    reply = fallback(snapshot(shown={h["id"] for h in HABITS}), BEDTIME)
    assert (reply.text, reply.mapped_by, reply.turn.recommendations) == (FALLBACK_NONE, "manual", {})


def test_a_full_turn_researches_recommends_and_restores_names(remote, llm_reply):
    llm_reply(
        call("research", problem="stalling at bedtime"),
        call("recommend_habits", quests=[pick("bedtime", "Child A needs a clear end to the day.")]),
        f"Child A may want more time with you [{REF}]. The card below can help.",
        '{"poor_fit": []}',
    )
    history = [{"role": "coach", "content": "Hello Alex"}]
    reply = run_turn(remote, snapshot(), history, BEDTIME, ["Alex", "Maya"])
    assert reply.mapped_by == "ai" and reply.text.startswith("Alex may want more time")
    assert reply.turn.recommendations == {"bedtime": "Alex needs a clear end to the day."}
    assert list(reply.turn.sources) == [REF]
    sent = json.dumps(llm_reply.seen)
    assert "Alex" not in sent and "Child A keeps stalling" in sent
    assert llm_reply.seen[0]["model"] == "vendor/model"


def test_a_poor_fit_is_removed_and_the_agent_tries_once_more(remote, llm_reply):
    llm_reply(
        call("recommend_habits", quests=[pick("bedtime"), pick("wash-hands")]),
        "Here are two habits.",
        '{"poor_fit": ["wash-hands", "unknown"]}',
        call("recommend_habits", quests=[pick("story-time")]),
        "A book helps. Here is a better pick.",
    )
    snap = snapshot()
    reply = run_turn(remote, snap, [], BEDTIME, ["Alex"])
    assert reply.turn.recommendations == {"story-time": "It helps."}
    assert "wash-hands" in snap.shown and reply.text.startswith("A book helps")
    assert "Wash hands" in json.dumps(llm_reply.seen[3]["messages"])
    assert len(llm_reply.seen) == 5  # the second answer is not checked again


def test_a_failed_fit_check_keeps_the_picks(remote, llm_reply):
    llm_reply(
        call("recommend_habits", quests=[pick("bedtime")]),
        "Here is one habit.",
        httpx.Response(400, json={"error": "no"}),
    )
    assert run_turn(remote, snapshot(), [], BEDTIME, ["Alex"]).turn.recommendations == {
        "bedtime": "It helps."
    }


def test_a_clarifying_question_skips_the_fit_check(remote, llm_reply):
    llm_reply("When does it happen?")
    reply = run_turn(remote, snapshot(), [], "Evenings are hard", ["Alex"])
    assert reply.text == "When does it happen?" and len(llm_reply.seen) == 1


def test_changes_restore_names_and_an_empty_answer_gets_a_default(remote, llm_reply):
    change = call("propose_plan_change", habit_id="homework", action="pause", reason="Child A needs a break.")
    llm_reply(change, "")
    reply = run_turn(remote, snapshot(), [], "Homework is too much", ["Alex"])
    assert reply.turn.changes[0].reason == "Alex needs a break."
    assert reply.text.startswith("I've put my suggestions")


def test_a_model_failure_falls_back_and_logs_only_the_status(remote, llm_reply, caplog):
    llm_reply(httpx.Response(401, json={"error": "bad key"}))
    reply = run_turn(remote, snapshot(), [], BEDTIME, ["Alex"])
    assert reply.mapped_by == "keywords" and "401" in caplog.text and "stalling" not in caplog.text


def test_a_turn_over_its_deadline_falls_back(remote, monkeypatch):
    release = threading.Event()

    class Slow:
        def invoke(self, *args):
            release.wait(5)

    monkeypatch.setattr(coach, "build_graph", lambda *args: Slow())
    late = remote.model_copy(update={"coach_timeout_seconds": 0.05})
    try:
        assert run_turn(late, snapshot(), [], BEDTIME, ["Alex"]).mapped_by == "keywords"
    finally:
        release.set()


def test_endless_tool_calls_stop_at_the_call_limit(remote, llm_reply):
    llm_reply(call("get_progress"))
    run_turn(remote, snapshot(), [], BEDTIME, ["Alex"])
    assert len(llm_reply.seen) <= coach.MODEL_CALLS


# ── The API ───────────────────────────────────────────────────────────────────


@pytest.fixture
def family(client, db):
    for user in (PARENT, OTHER):
        db.execute("INSERT INTO hq.users(id,name) VALUES(%s,'Test')", (user,))
    body = {"name": "Rivera", "pin": "1234", "display_name": "Sam", "role": "mother"}
    return client.post("/v1/families", json=body).json()["id"]


@pytest.fixture
def alex(client, family):
    return client.post(f"/v1/families/{family}/children", json={"name": "Alex", "age": 8}).json()["id"]


def say(client, child, text=BEDTIME):
    return client.post(f"/v1/children/{child}/coach/messages", json={"text": text})


def add_card(db, child, kind, habit="bedtime", cue=None):
    return db.execute(
        "INSERT INTO hq.ai_suggestions(child_id,source,kind,habit_id,cue_time,reason)"
        " VALUES(%s,'planner',%s,%s,%s,'Because.') RETURNING id",
        (child, kind, habit, cue),
    ).fetchone()[0]


def accept(client, card):
    return client.post(f"/v1/suggestions/{card}/accept", json={"pin": "1234"})


def in_plan(client, child):
    habits = client.get(f"/v1/children/{child}/plan").json()["habits"]
    return {h["habit_id"]: (h["cue_time"], h["status"]) for h in habits}


def test_a_message_saves_the_conversation_and_cards(client, alex):
    response = say(client, alex)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mapped_by"] == "keywords" and body["safety"] is None
    assert [m["role"] for m in body["messages"]] == ["parent", "coach"]
    assert [s["kind"] for s in body["suggestions"]] == ["add"]
    assert client.get(f"/v1/children/{alex}/coach/messages").json()["messages"] == body["messages"]


def test_refining_replaces_the_cards_with_different_habits(client, alex):
    first = say(client, alex).json()["suggestions"][0]["habit_id"]
    second = say(client, alex, "Something different for bedtime please").json()["suggestions"]
    assert second and first not in [s["habit_id"] for s in second]


def test_risky_text_is_never_stored(client, alex, db):
    body = say(client, alex, "He said he wants to die").json()
    assert body["safety"]["guidance"] == SAFETY_GUIDANCE and body["messages"] == []
    assert db.execute("SELECT count(*) FROM hq.coach_messages").fetchone()[0] == 0


def test_a_turn_uses_the_snapshot_and_saves_changes(settings, remote, db, alex, llm_reply):
    db.execute(
        "INSERT INTO hq.child_habits(id,child_id,habit_id,cue_time,days) VALUES(%s,%s,'homework','16:00','{mon}')",
        (CHILD_HABIT, alex),
    )
    db.execute(
        "INSERT INTO hq.completions(child_habit_id,day,status,decided_by,decided_at)"
        " VALUES(%s,hq.child_today(%s),'approved',%s,now())",
        (CHILD_HABIT, alex, PARENT),
    )
    llm_reply(
        call("get_progress"),
        call("propose_plan_change", habit_id="homework", action="retime", reason="Later.", cue_time="17:00"),
        "See the card.",
    )
    with TestClient(create_app(remote)) as c:
        c.headers["Authorization"] = "Bearer " + token(settings)
        body = say(c, alex).json()
    assert "approved_days" in json.dumps(llm_reply.seen)
    assert [(s["kind"], s["cue_time"]) for s in body["suggestions"]] == [("retime", "17:00")]


def test_new_chat_clears_the_thread(client, alex):
    say(client, alex)
    assert client.delete(f"/v1/children/{alex}/coach/messages").status_code == 204
    assert client.get(f"/v1/children/{alex}/coach/messages").json() == {"messages": [], "suggestions": []}


def test_the_daily_limit_stops_more_requests(settings, db, alex):
    limited = settings.model_copy(update={"ai_daily_limit": 1})
    with TestClient(create_app(limited)) as c:
        c.headers["Authorization"] = "Bearer " + token(settings)
        assert say(c, alex).status_code == 200
        assert say(c, alex).status_code == 429


def test_accepting_needs_the_pin_and_adds_the_habit(client, family, alex):
    card = say(client, alex).json()["suggestions"][0]
    assert client.get(f"/v1/families/{family}/suggestions").json()[0]["child_name"] == "Alex"
    assert client.post(f"/v1/suggestions/{card['id']}/accept", json={"pin": "9999"}).status_code == 403
    response = accept(client, card["id"])
    assert response.status_code == 200, response.text
    assert list(in_plan(client, alex)) == [card["habit_id"]]
    assert accept(client, card["id"]).status_code == 409
    assert client.post(f"/v1/suggestions/{card['id']}/dismiss").status_code == 409


def test_dismissing_leaves_the_plan_alone(client, alex):
    card = say(client, alex).json()["suggestions"][0]
    assert client.post(f"/v1/suggestions/{card['id']}/dismiss").json()["suggestions"] == []
    assert in_plan(client, alex) == {}


def test_plan_changes_apply_when_accepted(client, db, alex):
    client.put(f"/v1/children/{alex}/plan", json={"goals": ["bedtime"], "habits": [{"habit_id": "bedtime"}]})
    assert accept(client, add_card(db, alex, "retime", cue="19:45")).status_code == 200
    assert in_plan(client, alex)["bedtime"][0] == "19:45"
    assert accept(client, add_card(db, alex, "pause")).status_code == 200
    assert in_plan(client, alex)["bedtime"][1] == "paused"
    assert accept(client, add_card(db, alex, "resume")).status_code == 200
    assert in_plan(client, alex)["bedtime"][1] == "active"
    assert accept(client, add_card(db, alex, "remove")).status_code == 200
    assert in_plan(client, alex) == {}
    response = accept(client, add_card(db, alex, "pause"))
    assert response.status_code == 409 and "no longer" in response.json()["detail"]


def test_adding_rechecks_the_plan_rules(client, db, family, alex):
    client.put(f"/v1/children/{alex}/plan", json={"goals": ["bedtime"], "habits": [{"habit_id": "bedtime"}]})
    assert "already in the plan" in accept(client, add_card(db, alex, "add")).json()["detail"]
    younger = client.post(f"/v1/families/{family}/children", json={"name": "Mia", "age": 3}).json()["id"]
    older = db.execute("SELECT id FROM hq.habits WHERE min_age>3 ORDER BY tier,id LIMIT 1").fetchone()[0]
    assert "is for ages" in accept(client, add_card(db, younger, "add", older)).json()["detail"]
    locked = db.execute(
        "SELECT id FROM hq.habits WHERE tier=3 AND 8 BETWEEN min_age AND max_age ORDER BY id LIMIT 1"
    ).fetchone()[0]
    assert "not unlocked" in accept(client, add_card(db, alex, "add", locked)).json()["detail"]
    extras = db.execute(
        "SELECT id FROM hq.habits WHERE tier=1 AND id<>'bedtime' ORDER BY id LIMIT 6"
    ).fetchall()
    for (habit,) in extras[:5]:
        db.execute(
            "INSERT INTO hq.child_habits(child_id,habit_id,cue_time,days) VALUES(%s,%s,'08:00','{mon}')",
            (alex, habit),
        )
    assert "already has 6" in accept(client, add_card(db, alex, "add", extras[5][0])).json()["detail"]


def test_outsiders_cannot_reach_the_coach(client, settings, family, alex):
    card = say(client, alex).json()["suggestions"][0]["id"]
    headers = {"Authorization": "Bearer " + token(settings, OTHER)}
    assert client.get(f"/v1/children/{alex}/coach/messages", headers=headers).status_code == 404
    message = {"text": "hello"}
    assert (
        client.post(f"/v1/children/{alex}/coach/messages", json=message, headers=headers).status_code == 404
    )
    assert client.delete(f"/v1/children/{alex}/coach/messages", headers=headers).status_code == 404
    assert client.get(f"/v1/families/{family}/suggestions", headers=headers).status_code == 404
    pin = {"pin": "1234"}
    assert client.post(f"/v1/suggestions/{card}/accept", json=pin, headers=headers).status_code == 404
    assert client.post(f"/v1/suggestions/{card}/dismiss", headers=headers).status_code == 404


def test_a_kid_token_cannot_reach_the_coach(client, alex):
    kid = client.post(f"/v1/children/{alex}/kid-session").json()["access_token"]
    headers = {"Authorization": "Bearer " + kid}
    assert client.get(f"/v1/children/{alex}/coach/messages", headers=headers).status_code == 401


def test_keywords_use_the_whole_conversation(settings):
    history = [{"role": "parent", "content": BEDTIME}]
    reply = run_turn(settings, snapshot(), history, "Something different please", ["Alex"])
    assert reply.turn.recommendations and reply.mapped_by == "keywords"
