"""The lean schema: family isolation through row-level security, data rules, and the seeded catalog."""

from datetime import date
from uuid import UUID, uuid4

import psycopg
import pytest
from conftest import OTHER, PARENT

from habitquest_backend.db import transaction

CO_PARENT = UUID("33333333-3333-3333-3333-333333333333")
FAMILY_TABLES = ["families", "caregivers", "children", "pain_points", "child_goals", "child_habits"]
DAILY_TABLES = ["completions", "points_ledger"]


def family(db, user, name="Rivera"):
    """A family with one caregiver, a child on one habit, an approved completion and its points."""
    db.execute("INSERT INTO hq.users(id,name) VALUES(%s,%s) ON CONFLICT DO NOTHING", (user, name))
    fam, child, habit, done = uuid4(), uuid4(), uuid4(), uuid4()
    db.execute("INSERT INTO hq.families(id,name,timezone) VALUES(%s,%s,'Europe/London')", (fam, name))
    db.execute("INSERT INTO hq.caregivers VALUES(%s,%s,'Sam','mother')", (fam, user))
    db.execute("INSERT INTO hq.children(id,family_id,name,age) VALUES(%s,%s,'Alex',8)", (child, fam))
    point = db.execute(
        "INSERT INTO hq.pain_points(child_id,text,mapped_by,created_by) VALUES(%s,'bedtime is a battle','ai',%s)"
        " RETURNING id",
        (child, user),
    ).fetchone()[0]
    db.execute(
        "INSERT INTO hq.child_goals VALUES(%s,'bedtime',%s,'stalls at night','{bedtime}','active')",
        (child, point),
    )
    db.execute(
        "INSERT INTO hq.child_habits(id,child_id,habit_id,cue_time,days) VALUES(%s,%s,'bedtime','20:00','{mon}')",
        (habit, child),
    )
    db.execute(
        "INSERT INTO hq.completions VALUES(%s,%s,current_date,'approved',now(),%s,now())", (done, habit, user)
    )
    db.execute(
        "INSERT INTO hq.points_ledger(child_id,amount,reason,completion_id,created_by) VALUES(%s,10,'earned',%s,%s)",
        (child, done, user),
    )
    return {"family": fam, "child": child, "habit": habit, "completion": done}


def visible(settings, user, table):
    with transaction(settings, user) as conn:
        return conn.execute(f"SELECT count(*) AS n FROM hq.{table}").fetchone()["n"]


def test_each_family_sees_only_its_own_rows(settings, db):
    family(db, PARENT)
    family(db, OTHER, "Okafor")
    for table in FAMILY_TABLES + DAILY_TABLES:
        assert visible(settings, PARENT, table) == 1, table
        assert visible(settings, OTHER, table) == 1, table
        assert visible(settings, None, table) == 0, table
    assert visible(settings, PARENT, "users") == 1


def test_caregivers_share_a_family(settings, db):
    rivera = family(db, PARENT)
    db.execute("INSERT INTO hq.users(id,name) VALUES(%s,'Jo')", (CO_PARENT,))
    db.execute("INSERT INTO hq.caregivers VALUES(%s,%s,'Jo','father')", (rivera["family"], CO_PARENT))
    assert visible(settings, CO_PARENT, "children") == 1
    assert visible(settings, CO_PARENT, "caregivers") == 2
    assert visible(settings, CO_PARENT, "users") == 1  # names come from caregivers.display_name


def test_another_familys_rows_cannot_be_written(settings, db):
    okafor = family(db, OTHER, "Okafor")
    with transaction(settings, PARENT) as conn:
        assert conn.execute("UPDATE hq.children SET name='Taken'").rowcount == 0
        assert conn.execute("DELETE FROM hq.families").rowcount == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO hq.children(family_id,name,age) VALUES(%s,'Intruder',7)", (okafor["family"],)
            )


def test_the_catalog_and_points_ledger_cannot_be_rewritten(settings, db):
    family(db, PARENT)
    for statement in (
        "UPDATE hq.points_ledger SET amount=1000",
        "DELETE FROM hq.points_ledger",
        "UPDATE hq.habits SET points=1000",
        "INSERT INTO hq.goals(id,title,description) VALUES('x','x','x')",
    ):
        with transaction(settings, PARENT) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE hq.children SET age=2",
        "UPDATE hq.children SET age=14",
        "UPDATE hq.children SET buddy='dragon'",
        "UPDATE hq.children SET world='moon'",
        "UPDATE hq.children SET routine_times='[]'",
        "UPDATE hq.caregivers SET role='uncle'",
        "UPDATE hq.child_habits SET days='{someday}'",
        "UPDATE hq.child_habits SET days='{}'",
        "UPDATE hq.child_habits SET ended_on=started_on-1",
        "UPDATE hq.completions SET decided_at=NULL, decided_by=NULL",
        "UPDATE hq.completions SET status='submitted'",
        "UPDATE hq.completions SET decided_by=NULL",
        "UPDATE hq.points_ledger SET completion_id=NULL",
        "UPDATE hq.points_ledger SET amount=-10",
        "UPDATE hq.points_ledger SET reason='redeemed'",
        "UPDATE hq.points_ledger SET amount=0, reason='adjusted'",
    ],
)
def test_data_rules_are_enforced(db, statement):
    family(db, PARENT)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(statement)


def test_redemptions_need_a_note_and_a_negative_amount(db):
    rivera = family(db, PARENT)
    add = "INSERT INTO hq.points_ledger(child_id,amount,reason,note) VALUES(%s,%s,'redeemed',%s)"
    db.execute(add, (rivera["child"], -5, "30 minutes of tablet"))
    for amount, note in ((-5, None), (5, "ice cream")):
        with pytest.raises(psycopg.errors.CheckViolation):
            db.execute(add, (rivera["child"], amount, note))


def test_one_live_row_per_habit_and_one_completion_per_day(db):
    rivera = family(db, PARENT)
    again = (
        "INSERT INTO hq.child_habits(child_id,habit_id,cue_time,days) VALUES(%s,'bedtime','20:30','{tue}')"
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute(again, (rivera["child"],))
    db.execute("UPDATE hq.child_habits SET status='removed', ended_on=current_date")
    db.execute(again, (rivera["child"],))  # removed rows are history; the habit can be picked again
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute(
            "INSERT INTO hq.completions(child_habit_id,day) VALUES(%s,%s)", (rivera["habit"], date.today())
        )


def test_deleting_a_family_removes_all_its_data(db):
    family(db, PARENT)
    keep = family(db, OTHER, "Okafor")
    db.execute("DELETE FROM hq.families WHERE id<>%s", (keep["family"],))
    for table in FAMILY_TABLES + DAILY_TABLES:
        assert db.execute(f"SELECT count(*) FROM hq.{table}").fetchone()[0] == 1, table


def test_seeded_catalog_serves_every_goal_and_age(db):
    tiers = dict(db.execute("SELECT DISTINCT tier, points FROM hq.habits").fetchall())
    assert tiers == {1: 10, 2: 15, 3: 20}
    gaps = db.execute(
        "SELECT g.id, a FROM hq.goals g CROSS JOIN generate_series(3,13) a WHERE g.is_active AND NOT EXISTS("
        " SELECT 1 FROM hq.habits h WHERE h.goal_id=g.id AND h.is_active AND h.tier=1"
        " AND a BETWEEN h.min_age AND h.max_age)"
    ).fetchall()
    assert gaps == []  # a level-1 child of any age has a starter habit for every goal
    assert db.execute("SELECT count(*) FROM hq.habits").fetchone()[0] == 36
