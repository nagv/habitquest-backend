"""Spending points and the points history."""

from uuid import uuid4

import psycopg
import pytest
from conftest import OTHER, PARENT, token

CO_PARENT = "33333333-3333-3333-3333-333333333333"


@pytest.fixture
def home(client, db):
    """A family with Alex, who has 30 points from three approved bedtimes (10 each)."""
    for user in (PARENT, OTHER, CO_PARENT):
        db.execute("INSERT INTO hq.users(id,name) VALUES(%s,'Test')", (user,))
    family = client.post(
        "/v1/families", json={"name": "Rivera", "pin": "1234", "display_name": "Sam", "role": "mother"}
    ).json()
    alex = client.post(f"/v1/families/{family['id']}/children", json={"name": "Alex", "age": 8}).json()["id"]
    plan = client.put(
        f"/v1/children/{alex}/plan", json={"goals": ["bedtime"], "habits": [{"habit_id": "bedtime"}]}
    )
    habit = plan.json()["habits"][0]["id"]
    for days_ago in (3, 2, 1):
        done = db.execute(
            "INSERT INTO hq.completions(child_habit_id,day,status,decided_by,decided_at)"
            " VALUES(%s,current_date-%s,'approved',%s,now()) RETURNING id",
            (habit, days_ago, PARENT),
        ).fetchone()[0]
        db.execute(
            "INSERT INTO hq.points_ledger(child_id,amount,reason,completion_id,created_by)"
            " VALUES(%s,10,'earned',%s,%s)",
            (alex, done, PARENT),
        )
    kid = client.post(f"/v1/children/{alex}/kid-session").json()["access_token"]
    return {"family": family, "alex": alex, "kid": {"Authorization": "Bearer " + kid}}


def redeem(client, home, points, note="30 minutes of tablet", **headers):
    return client.post(
        f"/v1/children/{home['alex']}/redemptions", json={"points": points, "note": note}, **headers
    )


def test_spending_points_records_what_they_were_for(client, home):
    response = redeem(client, home, 25)
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["points"], body["entry"]["amount"], body["entry"]["note"]) == (
        5,
        -25,
        "30 minutes of tablet",
    )
    history = client.get(f"/v1/children/{home['alex']}/points").json()
    assert (history["points"], history["earned"], history["spent"]) == (5, 30, 25)
    newest, *earned = history["entries"]
    assert (newest["reason"], newest["note"], newest["by"], newest["habit_id"]) == (
        "redeemed",
        "30 minutes of tablet",
        "Sam",
        None,
    )
    assert [(e["reason"], e["amount"], e["kid_title"]) for e in earned] == [("earned", 10, "Sleepy time")] * 3
    assert earned[0]["completion_day"] > earned[1]["completion_day"]  # newest first


def test_points_cannot_go_below_zero(client, home):
    refused = redeem(client, home, 31)
    assert refused.status_code == 422 and refused.json()["detail"] == "Not enough points: 30 available"
    assert redeem(client, home, 30).json()["points"] == 0
    assert redeem(client, home, 1).status_code == 422


def test_spending_points_never_takes_sparks(client, home):
    redeem(client, home, 30)
    assert client.get(f"/v1/children/{home['alex']}/plan").json()["progress"]["sparks"] == 3


@pytest.mark.parametrize(
    "body",
    [
        {"points": 0, "note": "x"},
        {"points": -5, "note": "x"},
        {"points": 5, "note": ""},
        {"points": 5, "note": "x" * 201},
        {"points": 5},
    ],
)
def test_redemptions_are_validated(client, home, body):
    assert client.post(f"/v1/children/{home['alex']}/redemptions", json=body).status_code == 422


def test_each_caregiver_is_named_on_their_entries(client, settings, home):
    jo = {"Authorization": "Bearer " + token(settings, CO_PARENT)}
    client.post(
        "/v1/families/join",
        json={"invite_code": home["family"]["invite_code"], "display_name": "Jo", "role": "father"},
        headers=jo,
    )
    redeem(client, home, 5, note="Ice cream", headers=jo)
    assert client.get(f"/v1/children/{home['alex']}/points").json()["entries"][0]["by"] == "Jo"


def test_kids_see_their_own_points_without_caregiver_details(client, home):
    mine = client.get("/v1/kid/points", headers=home["kid"]).json()
    assert (mine["points"], len(mine["entries"])) == (30, 3) and "by" not in mine["entries"][0]
    assert redeem(client, home, 5, headers=home["kid"]).status_code == 401
    assert client.get("/v1/kid/points").status_code == 401


def test_the_history_can_be_shortened(client, home):
    assert len(client.get(f"/v1/children/{home['alex']}/points", params={"limit": 1}).json()["entries"]) == 1
    for limit in (0, 201):
        assert client.get(f"/v1/children/{home['alex']}/points", params={"limit": limit}).status_code == 422


def test_other_families_cannot_spend_or_see_points(client, settings, home):
    stranger = {"Authorization": "Bearer " + token(settings, OTHER)}
    assert redeem(client, home, 5, headers=stranger).status_code == 404
    assert client.get(f"/v1/children/{home['alex']}/points", headers=stranger).status_code == 404
    assert (
        client.post(f"/v1/children/{uuid4()}/redemptions", json={"points": 1, "note": "x"}).status_code == 404
    )


def test_the_database_refuses_a_negative_balance(db, home):
    spend = "INSERT INTO hq.points_ledger(child_id,amount,reason,note) VALUES(%s,%s,%s,'test')"
    db.execute(spend, (home["alex"], -20, "adjusted"))  # within the balance
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(spend, (home["alex"], -11, "redeemed"))
