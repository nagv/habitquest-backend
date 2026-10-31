"""Rewards, check-ins, surprises, kid extras, family settings and point multipliers."""

import json
from datetime import date, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from conftest import OTHER, PARENT, REAL_GOLDEN, token
from fastapi.testclient import TestClient

from habitquest_backend import daily, extras, rewards
from habitquest_backend.db import transaction
from habitquest_backend.main import create_app
from habitquest_backend.safety import KID_SAFETY_MESSAGE


@pytest.fixture
def family(client, db):
    for user in (PARENT, OTHER):
        db.execute("INSERT INTO hq.users(id,name) VALUES(%s,'Test')", (user,))
    body = {"name": "Rivera", "pin": "1234", "display_name": "Sam", "role": "mother"}
    return client.post("/v1/families", json=body).json()["id"]


@pytest.fixture
def alex(client, family):
    return client.post(f"/v1/families/{family}/children", json={"name": "Alex", "age": 8}).json()["id"]


@pytest.fixture
def kid(client, alex):
    token_value = client.post(f"/v1/children/{alex}/kid-session").json()["access_token"]
    return {"Authorization": "Bearer " + token_value}


def outsider(settings):
    return {"Authorization": "Bearer " + token(settings, OTHER)}


def give_points(db, child, amount):
    db.execute(
        "INSERT INTO hq.points_ledger(child_id,amount,reason,note) VALUES(%s,%s,'bonus','Test')",
        (child, amount),
    )


def reward_named(client, family, title):
    return next(r for r in client.get(f"/v1/families/{family}/rewards").json() if r["title"] == title)


def add_habit(db, child, habit="bedtime"):
    days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    return db.execute(
        "INSERT INTO hq.child_habits(child_id,habit_id,cue_time,days) VALUES(%s,%s,'20:00',%s) RETURNING id",
        (child, habit, days),
    ).fetchone()[0]


def today(db, child):
    return db.execute("SELECT hq.child_today(%s)", (child,)).fetchone()[0]


def complete(db, habit_row, day, status="approved"):
    decided = (PARENT, "now") if status == "approved" else (None, None)
    return db.execute(
        "INSERT INTO hq.completions(child_habit_id,day,status,decided_by,decided_at) VALUES(%s,%s,%s,%s,%s::timestamptz)"
        " RETURNING id",
        (habit_row, day, status, *decided),
    ).fetchone()[0]


# ── The shop ──────────────────────────────────────────────────────────────────


def test_new_families_get_the_default_shop_and_settings(client, family):
    found = client.get(f"/v1/families/{family}/rewards").json()
    assert len(found) == 8 and found[0]["kind"] == "mystery"
    assert client.get(f"/v1/families/{family}/settings").json() == {"app_limit": 0, "buddy_talk": True}


def test_parents_edit_the_shop_and_kids_see_it(client, family, kid):
    added = client.post(
        f"/v1/families/{family}/rewards", json={"title": "Pizza", "emoji": "🍕", "cost": 5, "kind": "toy"}
    )
    assert added.status_code == 201
    reward = added.json()["id"]
    assert client.patch(f"/v1/rewards/{reward}", json={"cost": 7}).json()["cost"] == 7
    assert client.get("/v1/kid/rewards", headers=kid).json()[0]["title"] == "Pizza"
    assert client.delete(f"/v1/rewards/{reward}").status_code == 204
    assert "Pizza" not in [r["title"] for r in client.get("/v1/kid/rewards", headers=kid).json()]


def test_a_request_holds_points_until_declined(client, db, family, alex, kid):
    give_points(db, alex, 100)
    reward = reward_named(client, family, "Pick what's for dinner")
    response = client.post("/v1/kid/reward-requests", json={"reward_id": reward["id"]}, headers=kid)
    assert response.status_code == 201, response.text
    assert response.json()["points"] == 20
    request = client.get(f"/v1/families/{family}/reward-requests").json()[0]
    assert request["child_name"] == "Alex"
    assert client.post(f"/v1/reward-requests/{request['id']}/decline").json()["points"] == 100
    assert client.post(f"/v1/reward-requests/{request['id']}/decline").status_code == 409
    assert client.get("/v1/kid/reward-requests", headers=kid).json()[0]["status"] == "declined"


def test_approving_needs_the_pin_and_adds_screen_time(client, db, family, alex, kid):
    give_points(db, alex, 100)
    reward = reward_named(client, family, "+15 minutes screen time")
    request = client.post("/v1/kid/reward-requests", json={"reward_id": reward["id"]}, headers=kid).json()[
        "request"
    ]
    url = f"/v1/reward-requests/{request['id']}/approve"
    assert client.post(url, json={"pin": "9999"}).status_code == 403
    assert client.post(url, json={"pin": "1234"}).json()["points"] == 40
    bonus = client.get("/v1/kid/extras", headers=kid).json()["screen_bonus"]
    assert list(bonus.values()) == [15]
    assert client.post(url, json={"pin": "1234"}).status_code == 409


def test_a_child_cannot_overspend(client, family, kid):
    reward = reward_named(client, family, "A new small toy")
    response = client.post("/v1/kid/reward-requests", json={"reward_id": reward["id"]}, headers=kid)
    assert response.status_code == 422 and "400 more points" in response.json()["detail"]


def test_a_mystery_prize_waits_for_a_grown_up(client, db, family, alex, kid, monkeypatch):
    give_points(db, alex, 40)
    monkeypatch.setattr(rewards.random, "choices", lambda items, weights: [rewards.MYSTERY_PRIZES[4]])
    reward = reward_named(client, family, "Mystery box")
    request = client.post("/v1/kid/reward-requests", json={"reward_id": reward["id"]}, headers=kid).json()[
        "request"
    ]
    assert (request["title"], request["minutes"], request["status"]) == (
        "+10 minutes screen time",
        10,
        "pending",
    )


def test_a_mystery_jackpot_pays_straight_away(client, db, family, alex, kid, monkeypatch):
    give_points(db, alex, 40)
    monkeypatch.setattr(rewards.random, "choices", lambda items, weights: [rewards.MYSTERY_PRIZES[5]])
    reward = reward_named(client, family, "Mystery box")
    body = client.post("/v1/kid/reward-requests", json={"reward_id": reward["id"]}, headers=kid).json()
    assert body["request"]["status"] == "approved" and body["points"] == 100


def test_kid_ledger_is_limited(settings, db, alex):
    with pytest.raises(psycopg.errors.CheckViolation), transaction(settings, kid=UUID(alex)) as conn:
        conn.execute("SELECT hq.kid_ledger(5000,'bonus','Too much')")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), transaction(settings, kid=UUID(alex)) as conn:
        conn.execute(
            "INSERT INTO hq.points_ledger(child_id,amount,reason,note) VALUES(%s,5,'bonus','x')", (alex,)
        )


# ── Check-ins ─────────────────────────────────────────────────────────────────


def test_a_check_in_reaches_the_parents_and_can_be_replaced(client, family, kid):
    client.post("/v1/kid/checkins", json={"mood": "okay"}, headers=kid)
    response = client.post(
        "/v1/kid/checkins",
        json={"mood": "great", "stickers": ["best"], "hardest": "homework", "note": "I liked it"},
        headers=kid,
    )
    assert response.status_code == 200, response.text
    assert client.get("/v1/kid/checkins/today", headers=kid).json()["mood"] == "great"
    found = client.get(f"/v1/families/{family}/checkins").json()
    assert len(found) == 1 and found[0]["child_name"] == "Alex" and found[0]["read_at"] is None
    assert client.post(f"/v1/families/{family}/checkins/read").json() == {"marked": 1}
    assert client.post(f"/v1/families/{family}/checkins/read").json() == {"marked": 0}


def test_an_unknown_sticker_is_refused(client, kid):
    assert (
        client.post("/v1/kid/checkins", json={"mood": "okay", "stickers": ["x"]}, headers=kid).status_code
        == 422
    )


def test_an_unsafe_note_is_refused_with_a_kind_message(client, family, kid):
    response = client.post("/v1/kid/checkins", json={"mood": "okay", "note": "you are so dumb"}, headers=kid)
    assert response.status_code == 422 and "kind words" in response.json()["detail"]
    assert client.get(f"/v1/families/{family}/checkins").json() == []


def test_a_risky_note_is_kept_and_flagged(client, family, kid):
    body = client.post("/v1/kid/checkins", json={"mood": "hard", "note": "he hits me"}, headers=kid).json()
    assert body["message"] == KID_SAFETY_MESSAGE
    assert client.get(f"/v1/families/{family}/checkins").json()[0]["flagged"] == "abuse"


def test_the_model_screens_notes_outside_a_transaction(settings, remote, alex, llm_reply):
    llm_reply('{"safe": false, "category": "unkind", "risk": false}')
    with TestClient(create_app(remote)) as c:
        c.headers["Authorization"] = "Bearer " + token(settings)
        headers = {
            "Authorization": "Bearer " + c.post(f"/v1/children/{alex}/kid-session").json()["access_token"]
        }
        assert (
            c.post("/v1/kid/checkins", json={"mood": "okay", "note": "a note"}, headers=headers).status_code
            == 422
        )


def test_the_coach_sees_check_ins_without_notes(settings, remote, client, alex, kid, llm_reply):
    client.post("/v1/kid/checkins", json={"mood": "hard", "note": "secret words"}, headers=kid)
    call = {"id": "c", "type": "function", "function": {"name": "get_progress", "arguments": "{}"}}
    llm_reply({"content": "", "tool_calls": [call]}, "Noted.")
    with TestClient(create_app(remote)) as c:
        c.headers["Authorization"] = "Bearer " + token(settings)
        c.post(f"/v1/children/{alex}/coach/messages", json={"text": "How are things going?"})
    sent = json.dumps(llm_reply.seen)
    assert '\\"mood\\": \\"hard\\"' in sent and "secret words" not in sent


# ── Surprises ─────────────────────────────────────────────────────────────────


def plant(client, family, **body):
    return client.post(f"/v1/families/{family}/surprises", json={"kind": "note", "day": "2020-01-01", **body})


def test_a_surprise_pays_once(client, family, kid):
    planted = plant(client, family, message="Well done!", points=20)
    assert planted.status_code == 201, planted.text
    assert len(client.get(f"/v1/families/{family}/surprises").json()) == 1
    waiting = client.get("/v1/kid/surprises", headers=kid).json()
    assert waiting[0]["message"] == "Well done!"
    opened = client.post(f"/v1/kid/surprises/{waiting[0]['id']}/open", headers=kid)
    assert opened.json()["points"] == 20
    assert client.post(f"/v1/kid/surprises/{waiting[0]['id']}/open", headers=kid).status_code == 409
    assert client.get("/v1/kid/surprises", headers=kid).json() == []
    assert client.get(f"/v1/families/{family}/surprises").json() == []


def test_a_note_without_points_opens_too(client, family, kid):
    surprise = plant(client, family, message="Hello").json()
    assert client.post(f"/v1/kid/surprises/{surprise['id']}/open", headers=kid).json()["points"] == 0


def test_a_surprise_for_one_child_stays_with_them(client, family, alex, kid):
    maya = client.post(f"/v1/families/{family}/children", json={"name": "Maya", "age": 6}).json()["id"]
    surprise = plant(client, family, child_id=maya).json()
    assert client.get("/v1/kid/surprises", headers=kid).json() == []
    assert client.post(f"/v1/kid/surprises/{surprise['id']}/open", headers=kid).status_code == 404


def test_surprise_rules(client, family, settings):
    assert plant(client, family, message="you are so dumb").status_code == 422
    assert plant(client, family, kind="double", points=5).status_code == 422
    assert plant(client, family, child_id=str(uuid4())).status_code == 404


def test_a_double_day_only_shows_on_its_day(client, db, family, alex, kid):
    plant(client, family, kind="double", day=(today(db, alex) + timedelta(days=1)).isoformat())
    assert client.get("/v1/kid/surprises", headers=kid).json() == []
    plant(client, family, kind="double", day=today(db, alex).isoformat())
    assert [s["kind"] for s in client.get("/v1/kid/surprises", headers=kid).json()] == ["double"]


# ── Multipliers ───────────────────────────────────────────────────────────────


def test_a_double_day_doubles_the_points(client, db, family, alex, kid):
    row = add_habit(db, alex)
    day = today(db, alex)
    plant(client, family, kind="double", day=day.isoformat())
    habit = client.get("/v1/kid/today", headers=kid).json()["habits"][0]
    assert (habit["multiplier"], habit["boosts"]) == (2, ["double"])
    done = complete(db, row, day, "submitted")
    assert client.post(f"/v1/completions/{done}/approve").json()["points"] == 20


def test_a_streak_and_the_golden_quest_stack_to_the_cap(client, db, family, alex, monkeypatch):
    monkeypatch.setattr(daily, "golden_habit", REAL_GOLDEN)
    row = add_habit(db, alex)
    day = today(db, alex)
    for back in (1, 2):
        complete(db, row, day - timedelta(days=back))
    plant(client, family, kind="double", day=day.isoformat())
    habit = client.get(f"/v1/children/{alex}/today").json()["habits"][0]
    assert sorted(habit["boosts"]) == ["double", "golden", "streak"] and habit["multiplier"] == 3


def test_a_gap_breaks_the_streak(client, db, family, alex):
    row = add_habit(db, alex)
    day = today(db, alex)
    complete(db, row, day - timedelta(days=2))
    assert client.get(f"/v1/children/{alex}/today").json()["habits"][0]["boosts"] == []


def test_a_day_with_no_scheduled_habits_has_no_boosts(settings, db, alex):
    with transaction(settings, PARENT) as conn:
        assert daily.boosts(conn, UUID(alex), date(2020, 1, 1)) == {}


# ── Kid extras and settings ───────────────────────────────────────────────────


def test_extras_record_discoveries_and_only_owned_outfits(client, db, alex, kid):
    for _ in range(2):
        body = client.patch(
            "/v1/kid/extras", json={"discover": "camp:tent", "level_seen": 2}, headers=kid
        ).json()
    assert body["discoveries"] == ["camp:tent"] and body["level_seen"] == 2
    assert client.patch("/v1/kid/extras", json={"wearing": "crown"}, headers=kid).status_code == 422
    db.execute("UPDATE hq.child_extras SET accessories='{crown}' WHERE child_id=%s", (alex,))
    assert client.patch("/v1/kid/extras", json={"wearing": "crown"}, headers=kid).json()["wearing"] == "crown"
    assert client.patch("/v1/kid/extras", json={"wearing": ""}, headers=kid).json()["wearing"] is None


def test_a_savings_goal_must_be_in_the_shop(client, family, kid):
    reward = reward_named(client, family, "Mystery box")
    assert (
        client.patch("/v1/kid/extras", json={"goal": reward["id"]}, headers=kid).json()["goal"]
        == reward["id"]
    )
    assert client.patch("/v1/kid/extras", json={"goal": str(uuid4())}, headers=kid).status_code == 404


def test_the_chest_waits_for_every_quest_and_opens_once(client, db, alex, kid, monkeypatch):
    assert client.post("/v1/kid/chest", headers=kid).status_code == 409
    row = add_habit(db, alex)
    assert client.post("/v1/kid/chest", headers=kid).status_code == 409
    complete(db, row, today(db, alex))
    monkeypatch.setattr(extras.random, "random", lambda: 0.1)
    monkeypatch.setattr(extras.random, "choice", lambda items: "crown")
    body = client.post("/v1/kid/chest", headers=kid).json()
    assert body["prize"] == {"kind": "accessory", "id": "crown", "name": "Golden crown"}
    assert body["extras"]["wearing"] == "crown"
    assert client.post("/v1/kid/chest", headers=kid).status_code == 409


def test_the_chest_can_pay_points(client, db, alex, kid, monkeypatch):
    complete(db, add_habit(db, alex), today(db, alex))
    monkeypatch.setattr(extras.random, "random", lambda: 0.9)
    monkeypatch.setattr(extras.random, "randint", lambda low, high: 3)
    body = client.post("/v1/kid/chest", headers=kid).json()
    assert body["prize"] == {"kind": "points", "points": 15} and body["points"] >= 15


def test_app_time_adds_up_and_old_days_drop(client, db, alex, kid):
    old = (date.today() - timedelta(days=40)).isoformat()
    db.execute("UPDATE hq.child_extras SET app_seconds=%s WHERE child_id=%s", (json.dumps({old: 5}), alex))
    client.post("/v1/kid/app-time", json={"seconds": 60}, headers=kid)
    body = client.post("/v1/kid/app-time", json={"seconds": 60}, headers=kid).json()
    assert list(body["app_seconds"].values()) == [120]


def test_parents_extend_time_with_the_pin(client, alex):
    url = f"/v1/children/{alex}/app-time/extend"
    assert client.post(url, json={"minutes": 10, "pin": "9999"}).status_code == 403
    assert client.post(url, json={"minutes": 10, "pin": "1234"}).status_code == 200
    assert list(client.get(f"/v1/children/{alex}/extras").json()["app_extra_minutes"].values()) == [10]


def test_settings_change_and_reach_kid_mode(client, family, kid):
    url = f"/v1/families/{family}/settings"
    assert client.patch(url, json={"app_limit": 7}).status_code == 422
    assert client.patch(url, json={"app_limit": 30}).json() == {"app_limit": 30, "buddy_talk": True}
    assert client.patch(url, json={"buddy_talk": False}).json() == {"app_limit": 30, "buddy_talk": False}
    assert client.get("/v1/kid/extras", headers=kid).json()["app_limit"] == 30


# ── Isolation ─────────────────────────────────────────────────────────────────


def test_another_family_reaches_none_of_this(client, settings, family, alex, kid):
    headers = outsider(settings)
    reward = reward_named(client, family, "Mystery box")["id"]
    assert client.get(f"/v1/families/{family}/rewards", headers=headers).status_code == 404
    assert client.patch(f"/v1/rewards/{reward}", json={"cost": 1}, headers=headers).status_code == 404
    assert client.delete(f"/v1/rewards/{reward}", headers=headers).status_code == 404
    assert client.get(f"/v1/families/{family}/reward-requests", headers=headers).status_code == 404
    assert client.get(f"/v1/families/{family}/checkins", headers=headers).status_code == 404
    assert client.post(f"/v1/families/{family}/checkins/read", headers=headers).status_code == 404
    assert client.get(f"/v1/families/{family}/surprises", headers=headers).status_code == 404
    assert plant(client.__class__(client.app, headers=headers), family).status_code == 404
    assert client.get(f"/v1/children/{alex}/extras", headers=headers).status_code == 404
    assert client.get(f"/v1/families/{family}/settings", headers=headers).status_code == 404
    extend = {"minutes": 5, "pin": "1234"}
    assert (
        client.post(f"/v1/children/{alex}/app-time/extend", json=extend, headers=headers).status_code == 404
    )


def test_a_kid_cannot_reach_another_familys_shop(client, settings, db, family, kid):
    db.execute("INSERT INTO hq.families(id,name) VALUES('55555555-5555-5555-5555-555555555555','Other')")
    other = db.execute(
        "SELECT id FROM hq.rewards WHERE family_id='55555555-5555-5555-5555-555555555555' LIMIT 1"
    ).fetchone()[0]
    response = client.post("/v1/kid/reward-requests", json={"reward_id": str(other)}, headers=kid)
    assert response.status_code == 404


def test_a_caregiver_token_cannot_use_kid_routes(client):
    assert client.get("/v1/kid/extras").status_code == 401
