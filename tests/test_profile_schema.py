import psycopg
import pytest
from conftest import OTHER, PARENT


def seed_child(db, parent, child_id):
    db.execute("INSERT INTO hq.households(id) VALUES(%s) ON CONFLICT DO NOTHING", (parent,))
    db.execute("INSERT INTO hq.memberships VALUES(%s,%s) ON CONFLICT DO NOTHING", (parent, parent))
    db.execute(
        "INSERT INTO hq.children(id,household_id,name,age_band) VALUES(%s,%s,'Kid','6-9')", (child_id, parent)
    )
    db.execute(
        "INSERT INTO hq.intakes(id,child_id,parent_id,data) VALUES(%s,%s,%s,'{}')",
        (child_id, child_id, parent),
    )


def as_role(db, role, parent=None):
    db.execute(f"SET LOCAL ROLE {role}")
    db.execute("SELECT set_config('app.parent_id',%s,true)", (str(parent or ""),))


def test_profile_columns_have_safe_defaults(db):
    seed_child(db, PARENT, PARENT)
    row = db.execute(
        "SELECT interests,accessibility,opted_in_topics,preferred_world,routine_schedule,profile_version "
        "FROM hq.children"
    ).fetchone()
    assert row == ([], [], [], "space", {}, 1)


def test_child_goals_are_parent_scoped(db):
    seed_child(db, PARENT, PARENT)
    seed_child(db, OTHER, OTHER)
    for parent in (PARENT, OTHER):
        db.execute(
            "INSERT INTO hq.child_goals VALUES(%s,%s,'bedtime','active',1,%s)", (parent, parent, parent)
        )
    with db.transaction():
        as_role(db, "hq_api", PARENT)
        rows = db.execute("SELECT child_id FROM hq.child_goals").fetchall()
    assert rows == [(PARENT,)]


def test_api_may_update_profile_columns_only(db):
    seed_child(db, PARENT, PARENT)
    with db.transaction():
        as_role(db, "hq_api", PARENT)
        db.execute("UPDATE hq.children SET preferred_world='forest',profile_version=2")
        db.execute("UPDATE hq.households SET timezone='Europe/London'")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with db.transaction():
            as_role(db, "hq_api", PARENT)
            db.execute("UPDATE hq.children SET household_id=%s", (OTHER,))


def test_published_releases_view_hides_unpublished(db):
    db.execute(
        "INSERT INTO hq.catalog_releases(id,built_by) VALUES(7,'00000000-0000-0000-0000-0000000000a1')"
    )
    with db.transaction():
        as_role(db, "hq_worker")
        rows = db.execute("SELECT id FROM hq.published_releases").fetchall()
    assert rows == [(1,)]


@pytest.mark.parametrize("role", ["hq_api", "hq_worker"])
def test_service_roles_can_lock_release_sources(db, role):
    with db.transaction():
        as_role(db, role, PARENT)
        db.execute("SELECT hq.lock_release(ARRAY['bedtime-prep'])")
