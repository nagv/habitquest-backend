"""The daily loop: today's habits, kid submissions, caregiver approvals, Sparks and points."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg
import pytest
from conftest import OTHER, PARENT, token

from habitquest_backend.db import transaction

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
TODAY = datetime.now(UTC).strftime("%a").lower()  # the family below uses UTC
NOT_TODAY = [d for d in DAYS if d != TODAY]


@pytest.fixture
def home(client, db):
    """A family (UTC) with Alex on three habits: two scheduled today, one not; and Maya with one."""
    for user in (PARENT, OTHER):
        db.execute("INSERT INTO hq.users(id,name) VALUES(%s,'Test')", (user,))
    family = client.post(
        "/v1/families", json={"name": "Rivera", "pin": "1234", "display_name": "Sam", "role": "mother"}
    ).json()["id"]
    alex = client.post(f"/v1/families/{family}/children", json={"name": "Alex", "age": 8}).json()["id"]
    maya = client.post(f"/v1/families/{family}/children", json={"name": "Maya", "age": 6}).json()["id"]
    plan = client.put(
        f"/v1/children/{alex}/plan",
        json={
            "goals": ["bedtime"],
            "habits": [
                {"habit_id": "bedtime", "cue_time": "20:00"},
                {"habit_id": "brush-teeth", "cue_time": "19:30"},
                {"habit_id": "story-time", "days": NOT_TODAY},
            ],
        },
    ).json()
    habits = {h["habit_id"]: h["id"] for h in plan["habits"]}
    maya_plan = client.put(
        f"/v1/children/{maya}/plan", json={"goals": ["meals"], "habits": [{"habit_id": "lunch"}]}
    ).json()
    return {
        "family": family,
        "alex": alex,
        "maya": maya,
        "habits": habits,
        "maya_lunch": maya_plan["habits"][0]["id"],
        "kid": kid_headers(client, alex),
        "maya_kid": kid_headers(client, maya),
    }


def kid_headers(client, child):
    session = client.post(f"/v1/children/{child}/kid-session").json()
    return {"Authorization": "Bearer " + session["access_token"]}


def submit(client, home, habit, kid="kid"):
    return client.post(
        "/v1/kid/completions", json={"child_habit_id": home["habits"].get(habit, habit)}, headers=home[kid]
    )


def test_today_lists_only_habits_scheduled_today(client, home):
    today = client.get("/v1/kid/today", headers=home["kid"]).json()
    assert today["day"] == datetime.now(UTC).date().isoformat()
    assert [(h["habit_id"], h["cue_time"], h["status"]) for h in today["habits"]] == [
        ("brush-teeth", "19:30", "todo"),
        ("bedtime", "20:00", "todo"),
    ]
    assert (today["child"]["name"], today["points"], today["progress"]["sparks"]) == ("Alex", 0, 0)
    assert today["habits"][0]["kid_title"] == "Sparkly teeth"
    client.patch(f"/v1/child-habits/{home['habits']['bedtime']}", json={"status": "paused"})
    assert [h["habit_id"] for h in client.get(f"/v1/children/{home['alex']}/today").json()["habits"]] == [
        "brush-teeth"
    ]


def test_submit_approve_earns_one_spark_and_the_points_once(client, db, home):
    assert submit(client, home, "bedtime").status_code == 200
    again = submit(client, home, "bedtime").json()  # a second tap changes nothing
    assert [h["status"] for h in again["habits"]] == ["todo", "submitted"]
    [waiting] = client.get(f"/v1/families/{home['family']}/approvals").json()
    assert (waiting["child_name"], waiting["habit_id"], waiting["points"]) == ("Alex", "bedtime", 10)
    decided = client.post(f"/v1/completions/{waiting['completion_id']}/approve").json()
    assert decided["completion"]["status"] == "approved"
    assert (decided["progress"]["sparks"], decided["points"]) == (1, 10)
    assert client.post(f"/v1/completions/{waiting['completion_id']}/approve").status_code == 409
    assert client.post(f"/v1/completions/{waiting['completion_id']}/reject").status_code == 409
    assert submit(client, home, "bedtime").status_code == 409
    assert client.get(f"/v1/families/{home['family']}/approvals").json() == []
    ledger = db.execute("SELECT amount, reason, created_by FROM hq.points_ledger").fetchall()
    assert ledger == [(10, "earned", PARENT)]
    kid_view = client.get("/v1/kid/today", headers=home["kid"]).json()
    assert (kid_view["points"], kid_view["habits"][1]["status"]) == (10, "approved")


def test_not_yet_lets_the_child_try_again(client, db, home):
    submit(client, home, "brush-teeth")
    [waiting] = client.get(f"/v1/families/{home['family']}/approvals").json()
    rejected = client.post(f"/v1/completions/{waiting['completion_id']}/reject").json()
    assert (rejected["completion"]["status"], rejected["points"]) == ("rejected", 0)
    assert client.get("/v1/kid/today", headers=home["kid"]).json()["habits"][0]["status"] == "rejected"
    retried = submit(client, home, "brush-teeth").json()
    assert retried["habits"][0]["status"] == "submitted"
    assert db.execute("SELECT decided_by, decided_at FROM hq.completions").fetchall() == [(None, None)]
    assert client.post(f"/v1/completions/{waiting['completion_id']}/approve").json()["points"] == 10


def test_kids_can_only_submit_their_own_habits_for_today(client, home):
    assert submit(client, home, "story-time").status_code == 404  # not scheduled today
    assert submit(client, home, home["maya_lunch"]).status_code == 404  # Maya's habit
    assert submit(client, home, str(uuid4())).status_code == 404
    client.patch(f"/v1/child-habits/{home['habits']['bedtime']}", json={"status": "removed"})
    assert submit(client, home, "bedtime").status_code == 404


def test_each_token_only_works_on_its_own_side(client, home):
    assert client.get("/v1/kid/today").status_code == 401
    assert client.post("/v1/kid/completions", json={"child_habit_id": str(uuid4())}).status_code == 401
    submit(client, home, "bedtime")
    [waiting] = client.get(f"/v1/families/{home['family']}/approvals").json()
    assert (
        client.post(f"/v1/completions/{waiting['completion_id']}/approve", headers=home["kid"]).status_code
        == 401
    )
    assert client.get(f"/v1/families/{home['family']}/approvals", headers=home["kid"]).status_code == 401


def test_other_families_cannot_see_or_decide(client, settings, home):
    submit(client, home, "bedtime")
    [waiting] = client.get(f"/v1/families/{home['family']}/approvals").json()
    stranger = {"Authorization": "Bearer " + token(settings, OTHER)}
    assert client.get(f"/v1/families/{home['family']}/approvals", headers=stranger).status_code == 404
    assert client.get(f"/v1/children/{home['alex']}/today", headers=stranger).status_code == 404
    assert (
        client.post(f"/v1/completions/{waiting['completion_id']}/approve", headers=stranger).status_code
        == 404
    )
    assert client.post(f"/v1/completions/{uuid4()}/reject").status_code == 404


def test_approvals_cover_every_child_oldest_first(client, home):
    submit(client, home, "bedtime")
    submit(client, home, home["maya_lunch"], kid="maya_kid")
    waiting = client.get(f"/v1/families/{home['family']}/approvals").json()
    assert [(w["child_name"], w["habit_id"]) for w in waiting] == [("Alex", "bedtime"), ("Maya", "lunch")]


def test_approving_the_fifth_spark_levels_up(client, db, home):
    db.execute(
        "INSERT INTO hq.completions(child_habit_id,day,status,decided_by,decided_at)"
        " SELECT %s,current_date-n,'approved',%s,now() FROM generate_series(1,4) n",
        (home["habits"]["story-time"], PARENT),
    )
    submit(client, home, "bedtime")
    [waiting] = client.get(f"/v1/families/{home['family']}/approvals").json()
    level = client.post(f"/v1/completions/{waiting['completion_id']}/approve").json()["progress"]
    assert (level["sparks"], level["level_name"], level["unlocked_tier"]) == (5, "Explorer", 2)


def test_the_database_holds_kids_to_submitting_today(settings, client, db, home):
    alex, bedtime = home["alex"], home["habits"]["bedtime"]
    yesterday = datetime.now(UTC).date() - timedelta(days=1)
    for statement, params in (
        (
            "INSERT INTO hq.completions(child_habit_id,day,status,decided_by,decided_at)"
            " VALUES(%s,current_date,'approved',%s,now())",
            (bedtime, PARENT),
        ),
        ("INSERT INTO hq.completions(child_habit_id,day) VALUES(%s,%s)", (bedtime, yesterday)),
        ("INSERT INTO hq.completions(child_habit_id,day) VALUES(%s,current_date)", (home["maya_lunch"],)),
        ("INSERT INTO hq.points_ledger(child_id,amount,reason,note) VALUES(%s,5,'adjusted','x')", (alex,)),
    ):
        with transaction(settings, kid=alex) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(statement, params)
    submit(client, home, "bedtime")
    with transaction(settings, kid=alex) as conn:  # a submitted completion cannot be approved by the kid
        assert conn.execute("UPDATE hq.completions SET status='approved'").rowcount == 0
    submit(client, home, home["maya_lunch"], kid="maya_kid")
    with transaction(settings, kid=alex) as conn:
        assert conn.execute("SELECT count(*) AS n FROM hq.completions").fetchone()["n"] == 1


@pytest.mark.parametrize("zone", ["Pacific/Kiritimati", "Etc/GMT+12"])
def test_today_follows_the_family_time_zone(client, settings, db, home, zone):
    db.execute("UPDATE hq.families SET timezone=%s", (zone,))
    day = client.get(f"/v1/children/{home['alex']}/today").json()["day"]
    assert day == datetime.now(ZoneInfo(zone)).date().isoformat()  # these two zones are 26 hours apart
