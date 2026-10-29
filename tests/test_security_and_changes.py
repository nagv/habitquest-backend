from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from conftest import CONFIRM, OTHER, PARENT, intake, queued, token
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from pydantic import ValidationError

from habitquest_backend.auth import verify_token
from habitquest_backend.schemas import ChildInput, IntakeInput
from habitquest_backend.worker import run_once


@pytest.mark.parametrize(
    "claims",
    [
        {"exp": 0},
        {"aud": "wrong"},
        {"iss": "wrong"},
        {"sub": "not-a-uuid"},
        {"role": "anon"},
        {"sub": None},
        {"iat": datetime.now(UTC) + timedelta(days=1)},
    ],
)
def test_auth_rejects_invalid_claims(settings, claims):
    with pytest.raises(HTTPException) as error:
        verify_token(token(settings, **claims), settings)
    assert error.value.status_code == 401


def test_auth_rejects_bad_signature(settings):
    with pytest.raises(HTTPException):
        verify_token(token(settings) + "bad", settings)


def test_jwks_path_validates_a_real_asymmetric_signature(settings, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    value = jwt.encode(
        {
            "sub": str(PARENT),
            "iat": now,
            "exp": now + timedelta(minutes=1),
            "aud": "authenticated",
            "iss": settings.jwt_issuer,
            "role": "authenticated",
        },
        key,
        algorithm="RS256",
    )
    monkeypatch.setattr(
        jwt.PyJWKClient, "get_signing_key_from_jwt", lambda self, t: SimpleNamespace(key=key.public_key())
    )
    assert verify_token(value, settings.model_copy(update={"jwt_secret": None})) == PARENT


WITHDRAW = (
    "UPDATE hq.habit_versions SET withdrawn_at=now(),withdrawn_reason='safety' WHERE habit_id='bedtime-prep'"
)
EMPTY_RELEASE = (
    "UPDATE hq.catalog_releases SET is_current=false;"
    "INSERT INTO hq.catalog_releases(id,built_by,published_by,published_at,is_current) "
    "VALUES(2,'00000000-0000-0000-0000-0000000000a1','00000000-0000-0000-0000-0000000000a2',now(),true)"
)


@pytest.mark.parametrize(
    "changes",
    [
        {"timezone": "Imaginary/Place"},
        {"timezone": "../invalid"},
        {"preferred_world": "unknown"},
        {"interests": ["space", "space"]},
        {"interests": ["astrology"]},
        {"routine_schedule": {"bedtime": {"cue": "25:10", "days": ["mon"]}}},
        {"routine_schedule": {"lunch": {"cue": "12:00", "days": ["mon"]}}},
        {"routine_schedule": {"bedtime": {"cue": "20:00", "days": []}}},
        {"routine_schedule": {"bedtime": {"cue": "20:00", "days": ["mon", "mon"]}}},
    ],
)
def test_invalid_profiles_are_rejected(changes):
    with pytest.raises(ValidationError):
        ChildInput(name="Alex", age_band="6-9", **changes)


@pytest.mark.parametrize("changes", [{"available_minutes": 0}, {"concerns": "hi"}])
def test_invalid_intakes_are_rejected(changes):
    with pytest.raises(ValidationError):
        IntakeInput(**{"concerns": "Improve routines", **changes})


def test_ready_and_child_validation(client):
    assert client.get("/health/ready").status_code == 200
    assert client.post("/v1/children", json={"name": "", "age_band": "6-9"}).status_code == 422


def ready(client, settings):
    child, record, run = queued(client)
    assert run_once(settings)
    assert client.get(f"/v1/plan-runs/{run}").json()["status"] == "ready"
    return child, record, run


@pytest.mark.parametrize(
    "sql",
    [
        WITHDRAW,
        EMPTY_RELEASE,
        "UPDATE hq.policy SET version=2",
        "UPDATE hq.proposals SET expires_at=now()-interval '1 second'",
        "UPDATE hq.intakes SET version=version+1",
        "UPDATE hq.proposals SET version=2",
    ],
)
def test_changed_sources_or_state_block_confirmation(client, settings, db, sql):
    _, _, run = ready(client, settings)
    db.execute(sql)
    assert client.post(f"/v1/proposals/{run}/confirm", json=CONFIRM).status_code == 409
    assert db.execute("SELECT count(*) FROM hq.plans").fetchone()[0] == 0


def test_confirmation_versions_and_goal_lifecycle(client, settings):
    child, _, run = ready(client, settings)
    assert (
        client.post(f"/v1/proposals/{run}/confirm", json={**CONFIRM, "expected_plan_version": 9}).status_code
        == 409
    )
    assert (
        client.post(
            f"/v1/proposals/{run}/confirm", json={**CONFIRM, "expected_proposal_version": 2}
        ).status_code
        == 409
    )
    plan = client.post(f"/v1/proposals/{run}/confirm", json=CONFIRM).json()
    assert plan["version"] == 1 and plan["release_id"] == 1
    assert client.get(f"/v1/children/{child['id']}/plan").json()["id"] == plan["id"]
    goals = client.get(f"/v1/children/{child['id']}/goals").json()
    assert {(g["goal_id"], g["status"], g["since_plan_version"]) for g in goals} == {
        ("bedtime", "active", 1),
        ("responsibility", "active", 1),
        ("meals", "active", 1),
    }


def test_concurrent_confirmation_creates_one_version(client, settings, db):
    _, _, run = ready(client, settings)

    def approve(_):
        return client.post(f"/v1/proposals/{run}/confirm", json=CONFIRM)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(approve, range(2)))
    assert [r.status_code for r in responses] == [200, 200]
    assert responses[0].json()["id"] == responses[1].json()["id"]
    assert db.execute("SELECT count(*) FROM hq.plans").fetchone()[0] == 1


def test_other_household_cannot_confirm_or_read_proposal(client, settings):
    child, _, run = ready(client, settings)
    client.headers["Authorization"] = "Bearer " + token(settings, OTHER)
    assert client.get(f"/v1/plan-runs/{run}").status_code == 404
    assert client.get(f"/v1/proposals/{run}").status_code == 404
    assert client.post(f"/v1/proposals/{run}/confirm", json=CONFIRM).status_code == 404
    assert client.get(f"/v1/children/{child['id']}/plan").status_code == 404


def test_unmatched_concerns_become_questions_without_invented_habits(client, settings):
    _, _, run = queued(client, concerns="Something needs to change")
    assert run_once(settings)
    data = client.get(f"/v1/proposals/{run}").json()["data"]
    assert data["goals"] == [] and data["active"] == []
    assert data["unmatched_phrases"] == ["Something needs to change"]
    assert "describe it another way" in data["questions"][0]


def test_idempotency_key_cannot_cross_intakes(client):
    child, _, _ = queued(client)
    _, second = intake(client, child=child)
    response = client.post(f"/v1/intakes/{second['id']}/plan-runs", headers={"Idempotency-Key": "first"})
    assert response.status_code == 409
