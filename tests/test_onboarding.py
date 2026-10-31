"""Children, pain points mapped to goals, and setting and adjusting a child's plan."""

from uuid import uuid4

import httpx
import pytest
from conftest import OTHER, PARENT, token
from pydantic import SecretStr

from habitquest_backend import ai, llm
from habitquest_backend.db import transaction
from habitquest_backend.main import create_app
from habitquest_backend.onboarding import progress

ALEX = {"name": "Alex", "age": 8, "buddy": "owl", "world": "forest", "routine_times": {"bedtime": "19:45"}}
BEDTIME = "Bedtime is a battle every night and he will not go to sleep"


@pytest.fixture
def family(client, db):
    for user in (PARENT, OTHER):
        db.execute("INSERT INTO hq.users(id,name) VALUES(%s,'Test')", (user,))
    body = {"name": "Rivera", "pin": "1234", "display_name": "Sam", "role": "mother"}
    return client.post("/v1/families", json=body).json()["id"]


@pytest.fixture
def alex(client, family):
    response = client.post(f"/v1/families/{family}/children", json=ALEX)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def outsider(settings):
    return {"Authorization": "Bearer " + token(settings, OTHER)}


def approve(db, child_id, count):
    """Gives a child `count` Sparks: approved completions of one habit on past days."""
    habit = db.execute(
        "INSERT INTO hq.child_habits(child_id,habit_id,cue_time,days,status,started_on,ended_on)"
        " VALUES(%s,'water','12:00','{mon}','removed',current_date-400,current_date) RETURNING id",
        (child_id,),
    ).fetchone()[0]
    db.execute(
        "INSERT INTO hq.completions(child_habit_id,day,status,decided_by,decided_at)"
        " SELECT %s,current_date-n,'approved',%s,now() FROM generate_series(1,%s) n",
        (habit, PARENT, count),
    )


# ── Children ──────────────────────────────────────────────────────────────────


def test_adding_listing_reading_and_editing_children(client, family, alex):
    child = client.get(f"/v1/children/{alex}").json()
    assert (child["name"], child["age"], child["buddy"], child["world"]) == ("Alex", 8, "owl", "forest")
    assert child["routine_times"] == {"bedtime": "19:45"} and child["family_id"] == family
    client.post(f"/v1/families/{family}/children", json={"name": "Maya", "age": 4})
    assert [c["name"] for c in client.get(f"/v1/families/{family}/children").json()] == ["Alex", "Maya"]
    edited = client.patch(
        f"/v1/children/{alex}", json={"age": 9, "routine_times": {"morning": "06:45"}}
    ).json()
    assert (edited["age"], edited["routine_times"], edited["buddy"]) == (9, {"morning": "06:45"}, "owl")


@pytest.mark.parametrize(
    "change",
    [
        {"age": 2},
        {"age": 14},
        {"buddy": "dragon"},
        {"world": "moon"},
        {"routine_times": {"noon": "12:00"}},
        {"routine_times": {"bedtime": "8pm"}},
        {"name": ""},
    ],
)
def test_child_input_is_validated(client, family, change):
    assert client.post(f"/v1/families/{family}/children", json={**ALEX, **change}).status_code == 422


def test_other_families_children_are_out_of_reach(client, settings, family, alex):
    stranger = outsider(settings)
    assert client.post(f"/v1/families/{family}/children", json=ALEX, headers=stranger).status_code == 404
    assert client.get(f"/v1/families/{family}/children", headers=stranger).status_code == 404
    for request in (
        lambda: client.get(f"/v1/children/{alex}", headers=stranger),
        lambda: client.patch(f"/v1/children/{alex}", json={"age": 5}, headers=stranger),
        lambda: client.post(f"/v1/children/{alex}/pain-points", json={"text": BEDTIME}, headers=stranger),
        lambda: client.get(f"/v1/children/{alex}/plan", headers=stranger),
        lambda: client.put(f"/v1/children/{alex}/plan", json={"goals": [], "habits": []}, headers=stranger),
    ):
        assert request().status_code == 404


# ── Pain points ───────────────────────────────────────────────────────────────


def test_a_pain_point_suggests_goals_with_recommended_habits(client, db, alex):
    response = client.post(f"/v1/children/{alex}/pain-points", json={"text": BEDTIME})
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["pain_point"]["mapped_by"], body["safety"]) == ("ai", None)
    [goal] = body["plan"]["goals"]
    assert (goal["goal_id"], goal["status"], goal["recommended_habit_ids"]) == (
        "bedtime",
        "suggested",
        ["brush-teeth", "bedtime"],
    )
    options = {h["id"]: h["recommended"] for h in goal["options"]}
    assert options == {"brush-teeth": True, "bedtime": True, "story-time": False}  # tier 1, ages fit
    assert body["plan"]["progress"] == {
        "sparks": 0,
        "level": 1,
        "level_name": "Sprout",
        "unlocked_tier": 1,
        "next_level_at": 5,
    }
    assert db.execute("SELECT text, created_by FROM hq.pain_points").fetchall() == [(BEDTIME, PARENT)]


def test_risky_text_returns_guidance_and_saves_nothing(client, db, alex):
    response = client.post(f"/v1/children/{alex}/pain-points", json={"text": "He says he wants to die"})
    assert response.status_code == 200
    assert response.json()["safety"]["guidance"] == ai.SAFETY_GUIDANCE and response.json()["plan"] is None
    assert db.execute("SELECT count(*) FROM hq.pain_points").fetchone()[0] == 0


def test_unrecognised_text_is_saved_for_manual_choice(client, alex):
    body = client.post(
        f"/v1/children/{alex}/pain-points", json={"text": "Everything feels harder lately"}
    ).json()
    assert body["pain_point"]["mapped_by"] == "manual" and body["plan"]["goals"] == []


def test_an_unavailable_model_falls_back_to_keywords(settings, db, alex, monkeypatch):
    remote = settings.model_copy(
        update={
            "ai_mode": "openai",
            "openai_base_url": "https://llm.example/v1",
            "openai_model": "m",
            "openai_api_key": SecretStr("k"),
        }
    )
    monkeypatch.setattr(
        llm,
        "http_client",
        lambda s: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))),
    )
    from fastapi.testclient import TestClient

    with TestClient(create_app(remote)) as c:
        c.headers["Authorization"] = "Bearer " + token(settings)
        body = c.post(f"/v1/children/{alex}/pain-points", json={"text": BEDTIME}).json()
    assert body["pain_point"]["mapped_by"] == "keywords"
    assert [g["recommended_habit_ids"] for g in body["plan"]["goals"]] == [[]]


def test_a_new_pain_point_keeps_active_goals_active(client, alex):
    client.put(f"/v1/children/{alex}/plan", json={"goals": ["bedtime"], "habits": [{"habit_id": "bedtime"}]})
    client.post(f"/v1/children/{alex}/pain-points", json={"text": BEDTIME + " and dinner is slow"})
    goals = {g["goal_id"]: g["status"] for g in client.get(f"/v1/children/{alex}/plan").json()["goals"]}
    assert goals == {"bedtime": "active", "meals": "suggested"}


# ── The plan ──────────────────────────────────────────────────────────────────


def test_setting_the_plan_activates_goals_and_habits_with_default_times(client, alex):
    client.post(f"/v1/children/{alex}/pain-points", json={"text": BEDTIME + " and dinner is slow"})
    plan = client.put(
        f"/v1/children/{alex}/plan",
        json={
            "goals": ["bedtime"],
            "habits": [
                {"habit_id": "bedtime"},
                {"habit_id": "story-time", "cue_time": "19:30", "days": ["mon", "wed"]},
            ],
        },
    ).json()
    assert [(g["goal_id"], g["status"]) for g in plan["goals"]] == [("bedtime", "active")]  # meals dropped
    habits = {h["habit_id"]: (h["cue_time"], h["days"], h["status"]) for h in plan["habits"]}
    assert habits == {
        "bedtime": ("19:45", ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], "active"),  # routine time
        "story-time": ("19:30", ["mon", "wed"], "active"),
    }
    assert (plan["habits"][0]["habit_id"], plan["habits"][0]["kid_title"]) == ("story-time", "Story time")


def test_changing_the_plan_keeps_history_and_times(client, db, alex):
    url = f"/v1/children/{alex}/plan"
    client.put(
        url,
        json={
            "goals": ["bedtime"],
            "habits": [{"habit_id": "bedtime", "cue_time": "20:15"}, {"habit_id": "story-time"}],
        },
    )
    plan = client.put(
        url,
        json={"goals": ["bedtime", "mornings"], "habits": [{"habit_id": "bedtime"}, {"habit_id": "wake"}]},
    ).json()
    assert {h["habit_id"]: h["cue_time"] for h in plan["habits"]} == {"wake": "07:00", "bedtime": "20:15"}
    history = db.execute(
        "SELECT habit_id, status, ended_on IS NOT NULL FROM hq.child_habits ORDER BY habit_id"
    ).fetchall()
    assert history == [
        ("bedtime", "active", False),
        ("story-time", "removed", True),
        ("wake", "active", False),
    ]
    client.put(url, json={"goals": ["bedtime"], "habits": [{"habit_id": "story-time"}]})
    assert db.execute("SELECT count(*) FROM hq.child_habits WHERE habit_id='story-time'").fetchone()[0] == 2


@pytest.mark.parametrize(
    ("plan", "message"),
    [
        ({"goals": ["flying"], "habits": []}, "Unknown goal: flying"),
        ({"goals": ["bedtime"], "habits": [{"habit_id": "nap"}]}, "Unknown habit: nap"),
        (
            {"goals": ["bedtime"], "habits": [{"habit_id": "wake"}]},
            "wake belongs to goal mornings, which is not listed",
        ),
        (
            {"goals": ["mornings"], "habits": [{"habit_id": "plan-tomorrow"}]},
            "plan-tomorrow is for ages 8-13",
        ),
        (
            {"goals": ["mornings"], "habits": [{"habit_id": "make-bed"}]},
            "make-bed is tier 2; this child has unlocked tier 1",
        ),
    ],
)
def test_plans_outside_the_rules_are_refused(client, family, plan, message):
    young = client.post(f"/v1/families/{family}/children", json={**ALEX, "age": 6}).json()["id"]
    response = client.put(f"/v1/children/{young}/plan", json=plan)
    assert response.status_code == 422 and response.json()["detail"] == message


@pytest.mark.parametrize(
    "plan",
    [
        {
            "goals": ["meals"],
            "habits": [
                {"habit_id": h}
                for h in ["lunch", "dinner", "water", "veggie", "wash-hands", "lunch", "dinner"]
            ],
        },
        {"goals": ["meals"], "habits": [{"habit_id": "lunch"}, {"habit_id": "lunch"}]},
        {"goals": ["meals", "meals"], "habits": []},
        {"goals": ["meals"], "habits": [{"habit_id": "lunch", "days": ["mon", "mon"]}]},
        {"goals": ["meals"], "habits": [{"habit_id": "lunch", "cue_time": "25:00"}]},
    ],
)
def test_plan_shape_is_validated(client, alex, plan):
    assert client.put(f"/v1/children/{alex}/plan", json=plan).status_code == 422


def test_sparks_unlock_harder_habits(client, db, alex):
    approve(db, alex, 5)
    plan = client.put(
        f"/v1/children/{alex}/plan", json={"goals": ["mornings"], "habits": [{"habit_id": "make-bed"}]}
    )
    assert plan.status_code == 200, plan.text
    assert plan.json()["progress"] == {
        "sparks": 5,
        "level": 2,
        "level_name": "Explorer",
        "unlocked_tier": 2,
        "next_level_at": 15,
    }


def test_the_top_level_has_no_next_level(settings, db, alex):
    approve(db, alex, 50)
    with transaction(settings, PARENT) as conn:
        assert progress(conn, alex) == {
            "sparks": 50,
            "level": 5,
            "level_name": "Legend",
            "unlocked_tier": 3,
            "next_level_at": None,
        }


def test_adjusting_pausing_and_removing_one_habit(client, settings, db, alex):
    plan = client.put(
        f"/v1/children/{alex}/plan", json={"goals": ["bedtime"], "habits": [{"habit_id": "bedtime"}]}
    ).json()
    url = f"/v1/child-habits/{plan['habits'][0]['id']}"
    changed = client.patch(url, json={"cue_time": "20:30", "days": ["fri", "sat"], "status": "paused"}).json()
    assert {k: changed["habits"][0][k] for k in ("cue_time", "days", "status")} == {
        "cue_time": "20:30",
        "days": ["fri", "sat"],
        "status": "paused",
    }
    assert client.patch(url, json={"status": "active"}, headers=outsider(settings)).status_code == 404
    assert client.patch(url, json={"status": "removed"}).json()["habits"] == []
    assert db.execute("SELECT ended_on IS NOT NULL FROM hq.child_habits").fetchone()[0]
    assert client.patch(url, json={"status": "active"}).status_code == 409
    assert client.patch(f"/v1/child-habits/{uuid4()}", json={"status": "active"}).status_code == 404
