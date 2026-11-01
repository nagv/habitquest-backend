"""The world journey: tier-weighted Sparks, the daily cap, landmarks, quest stars and parent controls."""

from datetime import date, timedelta
from pathlib import Path

import pytest
from conftest import OTHER, PARENT, token
from test_family_features import add_habit, complete, today
from test_onboarding import approve

from habitquest_backend import journey
from habitquest_backend.db import transaction
from habitquest_backend.journey import (
    DAILY_SPARKS,
    LANDMARKS,
    RARE_STAR_APPROVALS,
    STAR_APPROVALS,
    WORLDS,
    next_landmark,
    stars_for,
    world_at,
)


@pytest.fixture
def family(client, db):
    for user in (PARENT, OTHER):
        db.execute("INSERT INTO hq.users(id,name) VALUES(%s,'Test')", (user,))
    body = {"name": "Rivera", "pin": "1234", "display_name": "Sam", "role": "mother"}
    return client.post("/v1/families", json=body).json()["id"]


@pytest.fixture
def alex(client, family):
    return client.post(f"/v1/families/{family}/children", json={"name": "Alex", "age": 8}).json()["id"]


def kid_headers(client, alex):
    value = client.post(f"/v1/children/{alex}/kid-session").json()["access_token"]
    return {"Authorization": "Bearer " + value}


def spark_days(db, alex, habit, tier_count):
    """Approves `habit` on `tier_count` past days."""
    row = add_habit(db, alex, habit)
    day = today(db, alex)
    for n in range(1, tier_count + 1):
        complete(db, row, day - timedelta(days=n))


def test_worlds_climb_and_landmarks_sit_inside_their_world():
    assert [w["sparks"] for w in WORLDS] == [0, 30, 90, 180, 300, 450]
    for world, following in zip(WORLDS, [*WORLDS[1:], None], strict=True):
        spots = [mark["sparks"] for mark in world["landmarks"]]
        assert spots == sorted(spots) and spots[0] > world["sparks"]
        assert following is None or spots[-1] < following["sparks"]
        assert all(0 <= mark["x"] <= 100 and 0 <= mark["y"] <= 100 for mark in world["landmarks"])
    assert len(LANDMARKS) == sum(len(w["landmarks"]) for w in WORLDS)


@pytest.mark.parametrize(("per_day", "days_to_last_world"), [(3, 150), (4, 112), (DAILY_SPARKS, 75)])
def test_the_journey_lasts_months_however_big_the_plan(per_day, days_to_last_world):
    assert round(WORLDS[-1]["sparks"] / per_day) == days_to_last_world


def test_each_world_has_something_new_at_least_every_three_days_of_full_play():
    spots = [mark["sparks"] for mark in LANDMARKS.values()] + [w["sparks"] for w in WORLDS]
    gaps = [b - a for a, b in zip(sorted(spots), sorted(spots)[1:], strict=False)]
    assert max(gaps) <= 3 * DAILY_SPARKS


def test_the_ladder_helpers():
    assert world_at(0)[0] == 1 and world_at(29)[1]["id"] == "camp"
    assert world_at(30) == (2, WORLDS[1]) and world_at(9999)[0] == 6
    assert next_landmark(0)["id"] == "lantern" and next_landmark(3)["id"] == "map"
    assert next_landmark(30)["world"] == "mosswood" and next_landmark(9999) is None
    assert [stars_for(n) for n in (4, 5, 15, 29, 30)] == [0, 1, 2, 2, 3]


def test_a_harder_quest_earns_more_sparks(client, db, family, alex):
    spark_days(db, alex, "make-bed", 3)
    level = client.get(f"/v1/children/{alex}/journey").json()["progress"]
    assert level["sparks"] == 6


def test_the_daily_cap_counts_a_day_once(client, db, family, alex):
    day = today(db, alex) - timedelta(days=1)
    for habit in ("make-bed", "set-table", "read-20", "kind-act"):
        complete(db, add_habit(db, alex, habit), day)
    assert client.get(f"/v1/children/{alex}/journey").json()["progress"]["sparks"] == DAILY_SPARKS


def test_waiting_quests_show_as_pending_within_the_cap(client, db, family, alex):
    day = today(db, alex)
    complete(db, add_habit(db, alex, "make-bed"), day, "submitted")
    level = client.get(f"/v1/children/{alex}/journey").json()["progress"]
    assert (level["sparks"], level["pending_sparks"]) == (0, 2)
    for habit in ("set-table", "read-20"):
        complete(db, add_habit(db, alex, habit), day)
    complete(db, add_habit(db, alex, "kind-act"), day, "submitted")
    level = client.get(f"/v1/children/{alex}/journey").json()["progress"]
    assert (level["sparks"], level["pending_sparks"]) == (4, 2)


def test_rejected_quests_earn_nothing(client, db, family, alex):
    done = complete(db, add_habit(db, alex, "make-bed"), today(db, alex), "submitted")
    db.execute(
        "UPDATE hq.completions SET status='rejected',decided_by=%s,decided_at=now() WHERE id=%s",
        (PARENT, done),
    )
    level = client.get(f"/v1/children/{alex}/journey").json()["progress"]
    assert (level["sparks"], level["pending_sparks"]) == (0, 0)


MIGRATION = Path(__file__).parent.parent / "db/migrations/202611020001_world_journey.sql"


@pytest.mark.parametrize(
    ("old_sparks", "bonus", "level_seen"), [(3, 0, 1), (5, 25, 2), (15, 75, 3), (20, 70, 3), (100, 0, 3)]
)
def test_the_migration_keeps_children_where_they_were(db, alex, old_sparks, bonus, level_seen):
    """Runs the migration's own statement on a child with `old_sparks` approved quests under the old rules."""
    approve(db, alex, old_sparks)
    text = MIGRATION.read_text(encoding="utf-8")
    db.execute(text[text.index("WITH days AS") : text.index("UPDATE hq.children k")])
    row = db.execute(
        "SELECT journey_bonus,level_seen FROM hq.child_extras WHERE child_id=%s", (alex,)
    ).fetchone()
    assert tuple(row) == (bonus, level_seen)


def test_a_journey_bonus_keeps_a_child_where_they_were(client, db, family, alex):
    db.execute("UPDATE hq.child_extras SET journey_bonus=90 WHERE child_id=%s", (alex,))
    level = client.get(f"/v1/children/{alex}/journey").json()["progress"]
    assert (level["sparks"], level["world"], level["unlocked_tier"], level["level"]) == (90, "sky", 3, 3)


def test_the_journey_lists_worlds_and_found_landmarks(client, settings, db, family, alex):
    approve(db, alex, 3)
    headers = kid_headers(client, alex)
    assert (
        client.patch("/v1/kid/extras", json={"discover": "camp:lantern"}, headers=headers).status_code == 200
    )
    body = client.get("/v1/kid/journey", headers=headers).json()
    assert body["discoveries"] == ["camp:lantern"]
    assert len(body["worlds"]) == 6 and body["progress"]["next_landmark"]["id"] == "map"
    assert client.get(f"/v1/children/{alex}/journey").json()["discoveries"] == ["camp:lantern"]


def test_landmarks_wait_for_their_sparks(client, db, family, alex):
    headers = kid_headers(client, alex)
    early = client.patch("/v1/kid/extras", json={"discover": "camp:lantern"}, headers=headers)
    assert early.status_code == 409
    assert client.patch("/v1/kid/extras", json={"discover": "camp:nope"}, headers=headers).status_code == 422


def test_a_child_changes_their_own_buddy(client, db, family, alex):
    headers = kid_headers(client, alex)
    assert client.patch("/v1/kid/extras", json={"buddy": "bunny"}, headers=headers).status_code == 200
    assert client.get(f"/v1/children/{alex}").json()["buddy"] == "bunny"
    assert client.patch("/v1/kid/extras", json={"buddy": "dragon"}, headers=headers).status_code == 422


def test_a_parent_can_open_or_hold_back_a_tier(client, db, family, alex):
    url = f"/v1/children/{alex}"
    assert client.patch(url, json={"tier_override": 2}).json()["tier_override"] == 2
    assert client.get(f"{url}/journey").json()["progress"]["unlocked_tier"] == 2
    approve(db, alex, 40)
    client.patch(url, json={"tier_override": 1})
    assert client.get(f"{url}/journey").json()["progress"]["unlocked_tier"] == 1
    client.patch(url, json={"tier_override": 0})
    assert client.get(f"{url}/journey").json()["progress"]["unlocked_tier"] == 2
    assert client.patch(url, json={"tier_override": 4}).status_code == 422


def test_quest_stars_appear_in_the_journey_and_today(client, db, family, alex):
    spark_days(db, alex, "bedtime", STAR_APPROVALS[0])
    stars = client.get(f"/v1/children/{alex}/journey").json()["quest_stars"]
    assert [(q["habit_id"], q["approvals"], q["stars"]) for q in stars] == [("bedtime", 5, 1)]
    assert client.get(f"/v1/children/{alex}/today").json()["habits"][0]["stars"] == 1


def reach_gold(client, db, alex, habit):
    """Leaves `habit` one approval short of gold, then has a parent approve today's."""
    row = add_habit(db, alex, habit)
    day = today(db, alex)
    for n in range(1, STAR_APPROVALS[-1]):
        complete(db, row, day - timedelta(days=n))
    complete(db, row, day, "submitted")
    completion = db.execute(
        "SELECT id FROM hq.completions WHERE child_habit_id=%s AND day=%s", (row, day)
    ).fetchone()[0]
    response = client.post(f"/v1/completions/{completion}/approve")
    assert response.status_code == 200, response.text
    return response.json()


def cards(client, family):
    return [
        (c["source"], c["kind"], c["habit_id"])
        for c in client.get(f"/v1/families/{family}/suggestions").json()
    ]


def test_gold_suggests_a_harder_quest(client, db, family, alex):
    db.execute("UPDATE hq.child_extras SET journey_bonus=100 WHERE child_id=%s", (alex,))
    reach_gold(client, db, alex, "brush-teeth")
    [(source, kind, habit)] = cards(client, family)
    assert (source, kind) == ("mastery", "add") and habit != "brush-teeth"


def test_gold_offers_retiring_a_quest_when_nothing_harder_fits(client, db, family, alex):
    db.execute("UPDATE hq.children SET tier_override=1 WHERE id=%s", (alex,))
    reach_gold(client, db, alex, "brush-teeth")
    assert cards(client, family) == [("mastery", "remove", "brush-teeth")]


def test_approval_moves_the_child_into_their_new_world(client, db, family, alex):
    db.execute("UPDATE hq.child_extras SET journey_bonus=29 WHERE child_id=%s", (alex,))
    row = add_habit(db, alex, "bedtime")
    complete(db, row, today(db, alex), "submitted")
    completion = db.execute("SELECT id FROM hq.completions WHERE child_habit_id=%s", (row,)).fetchone()[0]
    assert client.post(f"/v1/completions/{completion}/approve").json()["progress"]["world"] == "mosswood"
    assert client.get(f"/v1/children/{alex}").json()["world"] == "mosswood"


def test_a_child_coming_back_gets_a_warm_welcome(client, db, family, alex):
    row = add_habit(db, alex, "bedtime")
    complete(db, row, today(db, alex) - timedelta(days=5))
    body = client.get(f"/v1/children/{alex}/today").json()
    assert body["welcome_back"] is True and "welcome" in body["habits"][0]["boosts"]


def test_a_short_break_is_not_a_comeback_and_keeps_the_streak(client, db, family, alex):
    row = add_habit(db, alex, "bedtime")
    day = today(db, alex)
    for n in (1, 2, 5, 6):
        complete(db, row, day - timedelta(days=n))
    body = client.get(f"/v1/children/{alex}/today").json()
    assert body["welcome_back"] is False and body["streak"] == 4


def test_three_missed_days_end_the_streak(client, db, family, alex):
    row = add_habit(db, alex, "bedtime")
    day = today(db, alex)
    for n in (1, 5):
        complete(db, row, day - timedelta(days=n))
    assert client.get(f"/v1/children/{alex}/today").json()["streak"] == 1


def test_the_journey_is_private_to_the_family(client, settings, alex):
    outsider = {"Authorization": "Bearer " + token(settings, OTHER)}
    assert client.get(f"/v1/children/{alex}/journey", headers=outsider).status_code == 404


def test_progress_works_inside_a_transaction(settings, alex):
    with transaction(settings, PARENT) as conn:
        assert journey.progress(conn, alex)["world"] == "camp"


# ── Quests on fixed days ──────────────────────────────────────────────────────


def weekly(db, alex, day):
    """A habit that runs only on `day`'s weekday."""
    row = add_habit(db, alex)
    db.execute("UPDATE hq.child_habits SET days=%s WHERE id=%s", ([day.strftime("%a").lower()], row))
    return row


def test_rest_days_do_not_break_a_streak(client, db, family, alex):
    day = today(db, alex)
    row = weekly(db, alex, day)
    for weeks in (1, 2, 3):
        complete(db, row, day - timedelta(days=7 * weeks))
    body = client.get(f"/v1/children/{alex}/today").json()
    assert body["streak"] == 3 and "streak" in body["habits"][0]["boosts"]


def test_three_missed_quest_days_still_end_a_streak(client, db, family, alex):
    day = today(db, alex)
    row = weekly(db, alex, day)
    complete(db, row, day - timedelta(days=21))
    complete(db, row, day - timedelta(days=7 * 8))
    assert client.get(f"/v1/children/{alex}/today").json()["streak"] == 1


def test_a_streak_bonus_needs_enough_quest_days(client, db, family, alex):
    day = today(db, alex)
    row = weekly(db, alex, day)
    complete(db, row, day - timedelta(days=7))
    assert "streak" not in client.get(f"/v1/children/{alex}/today").json()["habits"][0]["boosts"]


def test_a_weekly_quest_is_not_a_comeback(client, db, family, alex):
    day = today(db, alex)
    complete(db, weekly(db, alex, day), day - timedelta(days=7))
    assert client.get(f"/v1/children/{alex}/today").json()["welcome_back"] is False


def test_missing_weekly_quests_earns_a_welcome_back(client, db, family, alex):
    day = today(db, alex)
    complete(db, weekly(db, alex, day), day - timedelta(days=28))
    assert client.get(f"/v1/children/{alex}/today").json()["welcome_back"] is True


def test_a_day_with_no_quests_is_a_rest_day(client, db, family, alex):
    day = today(db, alex)
    weekly(db, alex, day + timedelta(days=2))
    body = client.get(f"/v1/children/{alex}/today").json()
    assert body["rest_day"] is True and body["habits"] == []
    assert date.fromisoformat(body["next_quest_day"]) == day + timedelta(days=2)


def test_a_child_with_no_plan_has_no_rest_day(client, family, alex):
    body = client.get(f"/v1/children/{alex}/today").json()
    assert body["rest_day"] is False and body["next_quest_day"] is None


def test_a_quest_day_is_not_a_rest_day(client, db, family, alex):
    weekly(db, alex, today(db, alex))
    body = client.get(f"/v1/children/{alex}/today").json()
    assert body["rest_day"] is False and body["next_quest_day"] is None


def test_quests_on_few_days_reach_gold_sooner(client, db, family, alex):
    day = today(db, alex)
    row = weekly(db, alex, day)
    for weeks in range(1, RARE_STAR_APPROVALS[-1]):
        complete(db, row, day - timedelta(days=7 * weeks))
    complete(db, row, day, "submitted")
    completion = db.execute(
        "SELECT id FROM hq.completions WHERE child_habit_id=%s AND day=%s", (row, day)
    ).fetchone()[0]
    client.post(f"/v1/completions/{completion}/approve")
    [quest] = client.get(f"/v1/children/{alex}/journey").json()["quest_stars"]
    assert (quest["stars"], quest["targets"]) == (3, list(RARE_STAR_APPROVALS))
    assert [source for source, _, _ in cards(client, family)] == ["mastery"]
