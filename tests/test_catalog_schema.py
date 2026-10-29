import psycopg
import pytest

AUTHOR = "00000000-0000-0000-0000-0000000000a1"
REVIEWER = "00000000-0000-0000-0000-0000000000a2"


def draft_habit(db, habit_id="test-habit"):
    db.execute("INSERT INTO hq.habits VALUES(%s)", (habit_id,))
    db.execute(
        "INSERT INTO hq.habit_versions(habit_id,version,title,instruction,min_age,max_age,routine_anchor,created_by) "
        "VALUES(%s,1,'Test habit','Do the agreed thing.',6,13,'bedtime',%s)",
        (habit_id, AUTHOR),
    )
    db.execute(
        "INSERT INTO hq.habit_levels(habit_id,version,level,minutes,points) VALUES(%s,1,'standard',5,1)",
        (habit_id,),
    )


def approve_habit(db, habit_id="test-habit"):
    db.execute(
        "UPDATE hq.habit_versions SET status='approved',reviewed_by=%s,reviewed_at=now() WHERE habit_id=%s",
        (REVIEWER, habit_id),
    )


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE hq.habit_versions SET title='Changed' WHERE habit_id='test-habit'",
        "INSERT INTO hq.habit_levels(habit_id,version,level,minutes,points) VALUES('test-habit',1,'simple',2,1)",
        "UPDATE hq.habit_levels SET minutes=9 WHERE habit_id='test-habit'",
        "DELETE FROM hq.habit_levels WHERE habit_id='test-habit'",
        "DELETE FROM hq.habit_versions WHERE habit_id='test-habit'",
    ],
)
def test_approved_habit_version_and_details_are_immutable(db, statement):
    draft_habit(db)
    approve_habit(db)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute(statement)


def test_approved_habit_version_can_only_be_withdrawn(db):
    draft_habit(db)
    approve_habit(db)
    db.execute(
        "UPDATE hq.habit_versions SET withdrawn_at=now(),withdrawn_reason='safety' WHERE habit_id='test-habit'"
    )
    assert db.execute(
        "SELECT withdrawn_reason FROM hq.habit_versions WHERE habit_id='test-habit'"
    ).fetchone() == ("safety",)


def test_draft_details_remain_editable(db):
    draft_habit(db)
    db.execute("UPDATE hq.habit_levels SET minutes=7 WHERE habit_id='test-habit'")
    db.execute("DELETE FROM hq.habit_levels WHERE habit_id='test-habit'")
    db.execute("DELETE FROM hq.habit_versions WHERE habit_id='test-habit'")
    assert db.execute("SELECT count(*) FROM hq.habit_versions WHERE habit_id='test-habit'").fetchone() == (0,)


def test_author_cannot_approve_own_version(db):
    draft_habit(db)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE hq.habit_versions SET status='approved',reviewed_by=created_by,reviewed_at=now()")


@pytest.mark.parametrize(
    "setup,statement",
    [
        (
            "INSERT INTO hq.goals(id,version,title,created_by) VALUES('test-goal',1,'Goal',%(a)s)",
            "UPDATE hq.goals SET title='Changed' WHERE id='test-goal'",
        ),
        (
            "INSERT INTO hq.concerns(id,version,label,created_by) VALUES('test-concern',1,'Concern',%(a)s)",
            "INSERT INTO hq.concern_goals VALUES('test-concern',1,'test-goal',1)",
        ),
        (
            "INSERT INTO hq.chapter_templates(id,version,world,goal_id,title,created_by) "
            "VALUES('test-chapter',1,'space','test-goal','Chapter',%(a)s)",
            "UPDATE hq.chapter_templates SET title='Changed' WHERE id='test-chapter'",
        ),
    ],
)
def test_other_approved_entities_are_immutable(db, setup, statement):
    db.execute(setup, {"a": AUTHOR})
    table = setup.split()[2].split("(")[0]
    db.execute(f"UPDATE {table} SET status='approved',reviewed_by=%s,reviewed_at=now()", (REVIEWER,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute(statement)


def test_draft_concern_goals_are_editable(db):
    db.execute(
        "INSERT INTO hq.concerns(id,version,label,created_by) VALUES('test-concern',1,'Concern',%s)",
        (AUTHOR,),
    )
    db.execute("INSERT INTO hq.concern_goals VALUES('test-concern',1,'test-goal',1)")
    db.execute("DELETE FROM hq.concern_goals WHERE concern_id='test-concern'")


def test_single_current_release_and_frozen_published_items(db):
    db.execute("INSERT INTO hq.catalog_releases(id,built_by) VALUES(10,%s),(11,%s)", (AUTHOR, AUTHOR))
    db.execute("INSERT INTO hq.release_items VALUES(11,'habit','x',1)")
    db.execute("DELETE FROM hq.release_items WHERE release_id=11")
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE hq.catalog_releases SET published_by=built_by,published_at=now() WHERE id=10")
    db.execute("UPDATE hq.catalog_releases SET is_current=false")
    db.execute(
        "UPDATE hq.catalog_releases SET published_by=%s,published_at=now() WHERE id IN (10,11)", (REVIEWER,)
    )
    db.execute("UPDATE hq.catalog_releases SET is_current=true WHERE id=10")
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute("UPDATE hq.catalog_releases SET is_current=true WHERE id=11")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute("INSERT INTO hq.release_items VALUES(10,'habit','x',1)")


@pytest.mark.parametrize("role", ["hq_api", "hq_worker"])
def test_service_roles_cannot_read_catalog_tables(db, role):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with db.transaction():
            db.execute(f"SET LOCAL ROLE {role}")
            db.execute("SELECT 1 FROM hq.habit_versions")
