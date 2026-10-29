"""Tests for catalog_requests upsert and worker integration."""

import hashlib

from conftest import queued

from habitquest_backend import worker
from habitquest_backend.worker import store_catalog_requests


def request_row(db, phrase):
    phrase_hash = hashlib.md5(phrase.encode()).hexdigest()
    return db.execute(
        "SELECT normalized_phrase,count FROM hq.catalog_requests WHERE phrase_hash=%s", (phrase_hash,)
    ).fetchone()


def test_store_catalog_requests_inserts_row(db):
    store_catalog_requests(db, ["never goes to sleep"])
    assert request_row(db, "never goes to sleep") == ("never goes to sleep", 1)


def test_store_catalog_requests_increments_count(db):
    phrase = "won't eat dinner"
    store_catalog_requests(db, [phrase])
    store_catalog_requests(db, [phrase])
    assert request_row(db, phrase) == (phrase, 2)


def test_store_catalog_requests_normalizes_whitespace(db):
    store_catalog_requests(db, ["  too   much   space  "])
    assert request_row(db, "too much space") == ("too much space", 1)


def test_store_catalog_requests_skips_empty(db):
    store_catalog_requests(db, ["   ", ""])
    assert db.execute("SELECT count(*) FROM hq.catalog_requests").fetchone() == (0,)


def test_store_catalog_requests_deduplicates_case(db):
    store_catalog_requests(db, ["Morning CHAOS"])
    store_catalog_requests(db, ["morning chaos"])
    assert request_row(db, "morning chaos") == ("morning chaos", 2)


def test_worker_stores_catalog_when_store_feedback_true(client, settings, db):
    """When store_feedback=True and unmatched phrases exist, they are persisted."""
    queued(client, concerns="dragon fire is a real problem here", store_feedback=True)
    worker.run_once(settings)
    # The fake mapper emits unmatched_phrases when no goal matches
    assert db.execute("SELECT count(*) FROM hq.catalog_requests").fetchone()[0] >= 1


def test_worker_skips_catalog_when_store_feedback_false(client, settings, db):
    """Default: store_feedback not set → catalog_requests stays empty."""
    queued(client, concerns="dragon fire is a real problem here")
    worker.run_once(settings)
    assert db.execute("SELECT count(*) FROM hq.catalog_requests").fetchone() == (0,)
