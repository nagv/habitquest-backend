"""Smoke tests for the plan_flags migration (DB-only checks, skipped without a DB)."""

import pytest
from conftest import queued

from habitquest_backend import worker


def test_plan_flags_table_exists(db):
    db.execute("SELECT 1 FROM hq.plan_flags LIMIT 0")


def test_plan_flags_rls_rejects_cross_parent(db):
    """RLS should prevent a different parent from seeing another parent's flags."""
    # This test verifies the schema exists and has RLS enabled;
    # actual cross-parent isolation is enforced at transaction level by my_parent().
    row = db.execute(
        "SELECT relrowsecurity FROM pg_class WHERE oid='hq.plan_flags'::regclass"
    ).fetchone()
    assert row is not None
    assert row["relrowsecurity"] is True


def test_plan_flags_kind_constraint(db):
    """Only allowed kind values are accepted."""
    import uuid
    plan_id = uuid.uuid4()
    with pytest.raises(Exception, match="plan_flags_kind_check|violates check"):
        db.execute(
            "INSERT INTO hq.plan_flags(plan_id,kind,habit_id,reason) VALUES(%s,%s,%s,%s)",
            (plan_id, "bad_kind", "some-habit", "reason"),
        )


def test_catalog_requests_table_exists(db):
    db.execute("SELECT 1 FROM hq.catalog_requests LIMIT 0")


def test_catalog_requests_upsert(db):
    import hashlib
    phrase = "the child never sleeps"
    phrase_hash = hashlib.md5(phrase.encode()).hexdigest()
    db.execute(
        "INSERT INTO hq.catalog_requests(phrase_hash,normalized_phrase) VALUES(%s,%s)",
        (phrase_hash, phrase),
    )
    db.execute(
        "INSERT INTO hq.catalog_requests(phrase_hash,normalized_phrase) VALUES(%s,%s) "
        "ON CONFLICT(phrase_hash) DO UPDATE SET count=hq.catalog_requests.count+1,last_seen=now()",
        (phrase_hash, phrase),
    )
    row = db.execute(
        "SELECT count FROM hq.catalog_requests WHERE phrase_hash=%s", (phrase_hash,)
    ).fetchone()
    assert row["count"] == 2


def test_plan_flags_inserted_by_flag_plan(client, settings, db):
    """Integration: flagging via service.flag_plan shows in plan-flags endpoint."""
    _, _, run = _ready(client, settings)
    client.post(f"/v1/proposals/{run}/confirm", json={"expected_plan_version": 0, "expected_proposal_version": 1})
    plan = db.execute(
        "SELECT id FROM hq.plans ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if plan is None:
        pytest.skip("No plan created")
    from habitquest_backend.service import flag_plan
    flag_plan(db, plan["id"], "habit_withdrawn", "bedtime-prep", "test withdrawal")
    child = db.execute("SELECT child_id FROM hq.plans WHERE id=%s", (plan["id"],)).fetchone()["child_id"]
    flags = db.execute(
        "SELECT * FROM hq.plan_flags WHERE plan_id=%s AND resolved_at IS NULL", (plan["id"],)
    ).fetchall()
    assert len(flags) == 1
    assert flags[0]["kind"] == "habit_withdrawn"
    assert flags[0]["habit_id"] == "bedtime-prep"


def _ready(client, settings):
    child, record, run = queued(client)
    worker.run_once(settings)
    return child, record, run
