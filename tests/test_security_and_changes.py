from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from conftest import OTHER, PARENT, onboard, queued, token
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from pydantic import ValidationError

from habitquest_backend.auth import verify_token
from habitquest_backend.schemas import OnboardingInput
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


@pytest.mark.parametrize(
    "changes",
    [
        {"goals": ["bedtime", "bedtime"]},
        {"timezone": "Imaginary/Place"},
        {"timezone": "../invalid"},
        {"bedtime": "25:10"},
        {"available_minutes": 0},
        {"world": "unknown"},
    ],
)
def test_invalid_onboarding_constraints_are_rejected(changes):
    with pytest.raises(ValidationError):
        OnboardingInput(concerns="Improve routines", **changes)


def test_ready_and_child_validation(client):
    assert client.get("/health/ready").status_code == 200
    assert client.post("/v1/children", json={"name": "", "age_band": "6-9"}).status_code == 422


def test_clarification_invalidates_goal_confirmation_and_old_request_key(client):
    _, session, run = queued(client)
    response = client.post(
        f"/v1/onboarding/{session['id']}/clarifications", json={**session["data"], "bedtime": "20:00"}
    )
    assert response.json()["version"] == 3
    assert not response.json()["goals_confirmed"]
    assert (
        client.post(
            f"/v1/onboarding/{session['id']}/confirm-goals",
            json={"expected_version": 2, "goals": ["bedtime"]},
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/v1/onboarding/{session['id']}/confirm-goals",
            json={"expected_version": 3, "goals": ["bedtime", "bedtime"]},
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/v1/onboarding/{session['id']}/confirm-goals",
            json={"expected_version": 3, "goals": ["bedtime"]},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/v1/onboarding/{session['id']}/plan-runs", headers={"Idempotency-Key": "first"}
        ).status_code
        == 409
    )


def ready(client, settings):
    child, session, run = queued(client)
    assert run_once(settings)
    assert client.get(f"/v1/plan-runs/{run}").json()["status"] == "ready"
    return child, session, run


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE hq.catalog SET approved=false WHERE id='bedtime-prep'",
        "UPDATE hq.catalog SET instruction='Changed after review' WHERE id='bedtime-prep'",
        "UPDATE hq.chapters SET approved=false WHERE id='space-bedtime'",
        "UPDATE hq.chapters SET title='Changed chapter' WHERE id='space-bedtime'",
        "UPDATE hq.policy SET version=2",
        "UPDATE hq.proposals SET expires_at=now()-interval '1 second'",
        "UPDATE hq.sessions SET version=version+1",
    ],
)
def test_changed_sources_or_state_block_confirmation(client, settings, db, sql):
    _, _, run = ready(client, settings)
    db.execute(sql)
    assert client.post(f"/v1/proposals/{run}/confirm", json={"expected_plan_version": 0}).status_code == 409
    assert db.execute("SELECT count(*) FROM hq.plans").fetchone()[0] == 0


def test_confirmation_version_and_preferences_lifecycle(client, settings):
    child, _, run = ready(client, settings)
    assert client.post(f"/v1/proposals/{run}/confirm", json={"expected_plan_version": 9}).status_code == 409
    plan = client.post(f"/v1/proposals/{run}/confirm", json={"expected_plan_version": 0}).json()
    assert client.get(f"/v1/children/{child['id']}/plan").json()["id"] == plan["id"]
    assert len(client.get(f"/v1/children/{child['id']}/preferences").json()) == 2
    response = client.put(
        f"/v1/children/{child['id']}/preferences",
        json={"kind": "world", "value": "forest", "source_id": plan["id"]},
    )
    assert response.status_code == 200
    assert response.json()["value"] == "forest"
    assert client.delete(f"/v1/children/{child['id']}/preferences/world").status_code == 204
    assert len(client.get(f"/v1/children/{child['id']}/preferences").json()) == 1


def test_concurrent_confirmation_creates_one_version(client, settings, db):
    _, _, run = ready(client, settings)

    def approve(_):
        return client.post(f"/v1/proposals/{run}/confirm", json={"expected_plan_version": 0})

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
    assert client.post(f"/v1/proposals/{run}/confirm", json={"expected_plan_version": 0}).status_code == 404
    assert client.get(f"/v1/children/{child['id']}/plan").status_code == 404


def test_goal_analysis_unknown_and_no_eligible_options_fail_safely(client, settings):
    _, session = onboard(client, concerns="Something needs to change", goals=[])
    response = client.post(
        f"/v1/onboarding/{session['id']}/analyze-runs", headers={"Idempotency-Key": "unknown"}
    )
    assert run_once(settings)
    analysis = client.get("/v1/plan-runs/" + response.json()["run_id"]).json()["analysis"]
    assert analysis["goals"] == [] and len(analysis["questions"]) == 1


def test_idempotency_key_cannot_cross_sessions_or_job_kinds(client):
    _, first, run = queued(client)
    _, second = onboard(client)
    assert (
        client.post(
            f"/v1/onboarding/{second['id']}/analyze-runs", headers={"Idempotency-Key": "first"}
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/v1/onboarding/{first['id']}/analyze-runs", headers={"Idempotency-Key": "first"}
        ).status_code
        == 409
    )
