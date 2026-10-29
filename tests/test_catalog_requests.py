"""Tests for catalog_requests upsert and worker integration."""

import hashlib

import pytest
from conftest import queued

from habitquest_backend import worker
from habitquest_backend.worker import store_catalog_requests


def test_store_catalog_requests_inserts_row(db):
    store_catalog_requests(db, ["never goes to sleep"])
    phrase_hash = hashlib.md5("never goes to sleep".encode()).hexdigest()
    row = db.execute(
        "SELECT * FROM hq.catalog_requests WHERE phrase_hash=%s", (phrase_hash,)
    ).fetchone()
    assert row is not None
    assert row["normalized_phrase"] == "never goes to sleep"
    assert row["count"] == 1


def test_store_catalog_requests_increments_count(db):
    phrase = "won't eat dinner"
    store_catalog_requests(db, [phrase])
    store_catalog_requests(db, [phrase])
    phrase_hash = hashlib.md5(phrase.encode()).hexdigest()
    row = db.execute(
        "SELECT count FROM hq.catalog_requests WHERE phrase_hash=%s", (phrase_hash,)
    ).fetchone()
    assert row["count"] == 2


def test_store_catalog_requests_normalizes_whitespace(db):
    store_catalog_requests(db, ["  too   much   space  "])
    phrase_hash = hashlib.md5("too much space".encode()).hexdigest()
    row = db.execute(
        "SELECT normalized_phrase FROM hq.catalog_requests WHERE phrase_hash=%s", (phrase_hash,)
    ).fetchone()
    assert row is not None
    assert row["normalized_phrase"] == "too much space"


def test_store_catalog_requests_skips_empty(db):
    store_catalog_requests(db, ["   ", ""])
    count = db.execute("SELECT count(*) AS n FROM hq.catalog_requests").fetchone()["n"]
    assert count == 0


def test_store_catalog_requests_deduplicates_case(db):
    store_catalog_requests(db, ["Morning CHAOS"])
    store_catalog_requests(db, ["morning chaos"])
    phrase_hash = hashlib.md5("morning chaos".encode()).hexdigest()
    row = db.execute(
        "SELECT count FROM hq.catalog_requests WHERE phrase_hash=%s", (phrase_hash,)
    ).fetchone()
    assert row["count"] == 2


def test_worker_stores_catalog_when_store_feedback_true(client, settings, db):
    """When store_feedback=True and unmatched phrases exist, they are persisted."""
    child, record, run = queued(client, concerns="dragon fire is a real problem here", store_feedback=True)
    worker.run_once(settings)
    rows = db.execute("SELECT * FROM hq.catalog_requests").fetchall()
    # The fake mapper emits unmatched_phrases when no goal matches
    assert len(rows) >= 1


def test_worker_skips_catalog_when_store_feedback_false(client, settings, db):
    """Default: store_feedback not set → catalog_requests stays empty."""
    child, record, run = queued(client, concerns="dragon fire is a real problem here")
    worker.run_once(settings)
    rows = db.execute("SELECT * FROM hq.catalog_requests").fetchall()
    assert len(rows) == 0
