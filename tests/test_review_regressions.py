import json

import psycopg
import pytest
from conftest import CONFIRM, OTHER, PARENT, add_child, queued, token
from test_security_and_changes import ready

from habitquest_backend import planning, worker
from habitquest_backend.db import transaction


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE hq.habit_versions SET withdrawn_at=now(),withdrawn_reason='safety' WHERE habit_id='clear-plate'",
        "UPDATE hq.catalog_releases SET is_current=false;"
        "INSERT INTO hq.catalog_releases(id,built_by,published_by,published_at,is_current) "
        "VALUES(2,'00000000-0000-0000-0000-0000000000a1','00000000-0000-0000-0000-0000000000a2',now(),true)",
    ],
)
def test_source_mutations_cannot_bypass_confirmation(client, settings, db, sql):
    _, _, run = ready(client, settings)
    db.execute(sql)
    response = client.post(f"/v1/proposals/{run}/confirm", json=CONFIRM)
    assert response.status_code == 409 and "replan_required" in response.json()["detail"]


def test_sql_role_cannot_enroll_itself_in_another_household(client, settings):
    add_child(client)
    client.headers["Authorization"] = "Bearer " + token(settings, OTHER)
    add_child(client)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with transaction(settings, PARENT) as conn:
            conn.execute("INSERT INTO hq.memberships VALUES(%s,%s)", (PARENT, OTHER))


def test_reviewer_receives_the_complete_candidate(client, settings, monkeypatch):
    _, _, run = queued(client)
    original = planning.BoundedChat._fake
    candidates = []

    def inspect(self, messages):
        if self.role == "reviewer":
            candidates.append(json.loads(messages[-1].content))
        return original(self, messages)

    monkeypatch.setattr(planning.BoundedChat, "_fake", inspect)
    worker.run_once(settings)
    assert candidates and "chapters" in candidates[0]["candidate"]
    assert "active" in candidates[0]["candidate"]


def test_checkpoint_namespace_changes_when_lease_is_reclaimed(client, settings, db):
    queued(client)
    first = worker.claim(settings)
    db.execute("UPDATE hq.jobs SET lease_until=now()-interval '1 second'")
    second = worker.claim(settings)
    assert "checkpoint_thread" in first
    assert first["checkpoint_thread"] != second["checkpoint_thread"]
    assert second["resume_thread"] == first["checkpoint_thread"]


def test_delayed_final_reviewer_cannot_publish_after_deadline(client, settings, db, monkeypatch):
    _, _, run = queued(client)
    original = planning.BoundedChat._fake

    def delay(self, messages):
        if self.role == "reviewer":
            db.execute("UPDATE hq.jobs SET deadline=now()-interval '1 second'")
        return original(self, messages)

    # Column absence currently exposes the missing durable deadline as a retry;
    # after implementation the final publication must fail closed.
    monkeypatch.setattr(planning.BoundedChat, "_fake", delay)
    worker.run_once(settings)
    assert client.get(f"/v1/plan-runs/{run}").json()["status"] == "failed"
