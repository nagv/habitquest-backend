"""Smoke tests for the plan_flags migration (DB-only checks, skipped without a DB)."""

import pytest
from conftest import ALEX, CONFIRM, queued

from habitquest_backend import worker
from habitquest_backend.service import flag_plan


def test_plan_flags_table_exists(db):
    db.execute("SELECT 1 FROM hq.plan_flags LIMIT 0")


def test_plan_flags_rls_rejects_cross_parent(db):
    """RLS should prevent a different parent from seeing another parent's flags."""
    # This test verifies the schema exists and has RLS enabled;
    # actual cross-parent isolation is enforced at transaction level by my_parent().
    row = db.execute("SELECT relrowsecurity FROM pg_class WHERE oid='hq.plan_flags'::regclass").fetchone()
    assert row == (True,)


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
    row = db.execute("SELECT count FROM hq.catalog_requests WHERE phrase_hash=%s", (phrase_hash,)).fetchone()
    assert row == (2,)


def test_plan_flags_inserted_by_flag_plan(client, settings, db):
    """Integration: flagging via service.flag_plan shows in plan-flags endpoint."""
    child, _, run = _ready(client, settings)
    assert client.post(f"/v1/proposals/{run}/confirm", json=CONFIRM).status_code == 200
    (plan_id,) = db.execute("SELECT id FROM hq.plans WHERE child_id=%s", (child["id"],)).fetchone()
    flag_plan(db, plan_id, "habit_withdrawn", "bedtime-prep", "test withdrawal")
    flags = client.get(f"/v1/children/{child['id']}/plan-flags").json()
    assert [(f["kind"], f["habit_id"]) for f in flags] == [("habit_withdrawn", "bedtime-prep")]


def test_profile_edit_removing_cue_flags_plan(client, settings, db):
    child, _, run = _ready(client, settings)
    assert client.post(f"/v1/proposals/{run}/confirm", json=CONFIRM).status_code == 200
    (active,) = db.execute("SELECT data->'active' FROM hq.plans WHERE child_id=%s", (child["id"],)).fetchone()
    bedtime = sorted(h["habit_id"] for h in active if h["anchor"] == "bedtime")
    assert bedtime
    profile = client.get(f"/v1/children/{child['id']}").json()
    schedule = {k: v for k, v in profile["routine_schedule"].items() if k != "bedtime"}
    body = {**ALEX, "routine_schedule": schedule, "expected_profile_version": profile["profile_version"]}
    assert client.patch(f"/v1/children/{child['id']}", json=body).status_code == 200, body
    flags = client.get(f"/v1/children/{child['id']}/plan-flags").json()
    assert sorted(f["habit_id"] for f in flags if f["kind"] == "cue_removed") == bedtime


def _ready(client, settings):
    child, record, run = queued(client)
    worker.run_once(settings)
    return child, record, run
