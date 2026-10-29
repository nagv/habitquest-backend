import os
import subprocess
import sys

import pytest
from conftest import queued

from habitquest_backend import planning, worker


def test_lease_renewal_loop_exits_when_fenced(client, settings):
    _, _, run = queued(client)
    job = worker.claim(settings)
    client.post(f"/v1/plan-runs/{run}/cancel")

    class Event:
        def wait(self, seconds):
            return False

    function = getattr(worker, "renew_loop", None)
    assert callable(function), "Extract a testable lease renewal loop"
    function(settings, job, Event())


def test_reviewer_revision_retrieves_evidence_again(client, settings, db, monkeypatch):
    _, _, run = queued(client)
    original = planning.BoundedChat._fake
    count = []

    def reject_once(self, messages):
        if self.role == "reviewer":
            count.append(1)
            if len(count) == 1:
                from langchain_core.messages import AIMessage

                return AIMessage(content='{"accepted":false,"reason":"Reconsider selection"}')
        return original(self, messages)

    monkeypatch.setattr(planning.BoundedChat, "_fake", reject_once)
    worker.run_once(settings)
    assert client.get(f"/v1/plan-runs/{run}").json()["status"] == "ready"
    assert db.execute("SELECT count(*) FROM hq.events WHERE kind='tool'").fetchone()[0] == 8


@pytest.mark.parametrize("incomplete", [False, True])
def test_checkpoint_recovery_reuses_durable_state(client, settings, db, monkeypatch, incomplete):
    _, _, run = queued(client)
    if incomplete:
        original = planning.BoundedChat._fake
        failures = []

        def crash(self, messages):
            from langchain_core.messages import ToolMessage

            if self.role == "director" and any(isinstance(m, ToolMessage) for m in messages) and not failures:
                failures.append(1)
                raise ConnectionError("interrupted model transport")
            return original(self, messages)

        monkeypatch.setattr(planning.BoundedChat, "_fake", crash)
    else:
        original = worker.generate

        def crash(*args):
            original(*args)
            raise ConnectionError("crash before durable result publication")

        monkeypatch.setattr(worker, "generate", crash)
    worker.run_once(settings)
    assert client.get(f"/v1/plan-runs/{run}").json()["status"] == "queued"
    if not incomplete:
        monkeypatch.setattr(worker, "generate", original)
    db.execute("UPDATE hq.jobs SET available_at=now()")
    worker.run_once(settings)
    assert client.get(f"/v1/plan-runs/{run}").json()["status"] == "ready"
    assert db.execute("SELECT count(*) FROM hq.proposals").fetchone()[0] == 1


def test_cli_once_runs_actual_worker_process(client, settings):
    _, _, run = queued(client)
    result = subprocess.run(
        [sys.executable, "-m", "habitquest_backend.worker", "--once"],
        env={
            **os.environ,
            "DATABASE_URL": settings.database_url,
            "MODEL_MODE": "fake",
            "LANGSMITH_TRACING": "false",
        },
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr
    assert client.get(f"/v1/plan-runs/{run}").json()["status"] == "ready"


def test_no_eligible_habit_yields_questions_not_invented_habits(client, settings, db):
    _, _, run = queued(client)
    db.execute("UPDATE hq.habit_versions SET withdrawn_at=now(),withdrawn_reason='test'")
    worker.run_once(settings)
    assert client.get(f"/v1/plan-runs/{run}").json()["status"] == "ready"
    data = client.get(f"/v1/proposals/{run}").json()["data"]
    assert data["active"] == [] and set(data["unresolved"].values()) == {"no_catalog_entry"}
