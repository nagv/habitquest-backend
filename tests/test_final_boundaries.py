import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from types import SimpleNamespace

import pytest
from conftest import CONFIRM, queued
from langchain_core.messages import AIMessage, HumanMessage
from test_security_and_changes import ready

from habitquest_backend import planning, service, worker


def test_content_edit_waits_until_confirmation_commits(client, settings, db, monkeypatch):
    _, _, run = ready(client, settings)
    validated = threading.Event()
    release = threading.Event()
    original = service.validate_sources

    def pause(conn, data):
        checked = original(conn, data)
        validated.set()
        assert release.wait(5)
        return checked

    monkeypatch.setattr(service, "validate_sources", pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        confirmation = pool.submit(lambda: client.post(f"/v1/proposals/{run}/confirm", json=CONFIRM))
        assert validated.wait(5)
        edit = pool.submit(
            lambda: db.execute(
                "UPDATE hq.habit_versions SET withdrawn_at=now(),withdrawn_reason='safety' WHERE habit_id='bedtime-prep'"
            )
        )
        try:
            with pytest.raises(TimeoutError):
                edit.result(timeout=0.2)
        finally:
            release.set()
        assert confirmation.result(timeout=5).status_code == 200
        edit.result(timeout=5)


def test_publication_rolls_back_when_deadline_passes_after_source_check(client, settings, db, monkeypatch):
    _, _, run = queued(client)
    original = worker.validate_sources

    def expire(conn, data):
        checked = original(conn, data)
        conn.execute("UPDATE hq.jobs SET deadline=clock_timestamp()-interval '1 second'")
        return checked

    monkeypatch.setattr(worker, "validate_sources", expire)
    worker.run_once(settings)
    assert client.get(f"/v1/plan-runs/{run}").json()["status"] == "failed"
    assert db.execute("SELECT count(*) FROM hq.proposals").fetchone()[0] == 0


RELEASE_WITHOUT_CHAPTERS = (
    "INSERT INTO hq.catalog_releases(id,built_by) VALUES(2,'00000000-0000-0000-0000-0000000000a1');"
    "INSERT INTO hq.release_items SELECT 2,entity,entity_id,version FROM hq.release_items "
    "WHERE release_id=1 AND entity<>'chapter';"
    "UPDATE hq.catalog_releases SET is_current=false WHERE id=1;"
    "UPDATE hq.catalog_releases SET published_by='00000000-0000-0000-0000-0000000000a2',"
    "published_at=now(),is_current=true WHERE id=2"
)


def test_harmful_concerns_are_blocked_before_planning(client, settings):
    _, _, run = queued(client, concerns="punish the child")
    worker.run_once(settings)
    assert client.get(f"/v1/plan-runs/{run}").json()["status"] == "failed"


def test_missing_chapter_is_not_replaced_with_invented_content(client, settings, db):
    _, _, run = queued(client)
    db.execute(RELEASE_WITHOUT_CHAPTERS)
    worker.run_once(settings)
    assert client.get(f"/v1/plan-runs/{run}").json()["status"] == "failed"


@pytest.mark.parametrize("clock", ["elapsed", "deadline"])
def test_slow_model_response_is_rejected_after_it_returns(settings, monkeypatch, clock):
    def delayed(self, messages):
        if clock == "elapsed":
            self.started -= 1000
        else:
            self.deadline = time.time() - 1
        return AIMessage(content='{"accepted":true,"reason":"late"}')

    monkeypatch.setattr(planning.BoundedChat, "_fake", delayed)
    model = planning.BoundedChat(
        role="reviewer", settings=settings, context={}, charge=lambda *a: None, started=time.monotonic()
    )
    with pytest.raises(planning.BudgetExceeded):
        model.invoke([HumanMessage(content="review")])


def test_lease_renewal_repeats_then_stops_and_handles_database_failure(client, settings):
    queued(client)
    job = worker.claim(settings)

    class Event:
        def __init__(self):
            self.calls = 0

        def wait(self, seconds):
            self.calls += 1
            return self.calls > 2

    event = Event()
    worker.renew_loop(settings, job, event)
    assert event.calls == 3
    broken = settings.model_copy(
        update={"database_url": "postgresql://invalid@127.0.0.1:1/none?connect_timeout=1"}
    )
    worker.renew_loop(broken, job, Event())


def test_polling_worker_processes_then_idles(client, settings, monkeypatch):
    _, _, run = queued(client)

    def stop(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(sys, "argv", ["hq-worker"])
    monkeypatch.setattr(worker, "Settings", lambda: settings)
    monkeypatch.setattr(worker, "time", SimpleNamespace(sleep=stop))
    with pytest.raises(KeyboardInterrupt):
        worker.main()
    assert client.get(f"/v1/plan-runs/{run}").json()["status"] == "ready"


def test_retry_without_a_checkpoint_starts_cleanly(client, settings, db, monkeypatch):
    _, _, run = queued(client)
    original = worker.generate

    def crash(*args):
        raise ConnectionError("transport failed before checkpoint")

    monkeypatch.setattr(worker, "generate", crash)
    worker.run_once(settings)
    monkeypatch.setattr(worker, "generate", original)
    db.execute("UPDATE hq.jobs SET available_at=now()")
    worker.run_once(settings)
    assert client.get(f"/v1/plan-runs/{run}").json()["status"] == "ready"
