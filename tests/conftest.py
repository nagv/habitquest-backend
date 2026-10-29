import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import jwt
import psycopg
import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict

from habitquest_backend.config import Settings
from habitquest_backend.main import create_app

SECRET = "test-only-signing-key-at-least-32-bytes-long"
PARENT = UUID("11111111-1111-1111-1111-111111111111")
OTHER = UUID("22222222-2222-2222-2222-222222222222")
load_dotenv()
URL = os.getenv("TEST_DATABASE_URL", "postgresql://hq_dev@127.0.0.1:55432/hq_test")
if not conninfo_to_dict(URL).get("dbname", "").endswith("_test"):
    raise pytest.UsageError(
        "TEST_DATABASE_URL must name a dedicated database ending in _test; fixtures delete test data."
    )


@pytest.fixture
def settings():
    return Settings(
        database_url=URL,
        jwt_secret=SECRET,
        jwt_issuer="http://local-test",
        max_run_seconds=30,
        model_mode="fake",
    )


def token(settings, user=PARENT, **overrides):
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": str(user),
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "aud": "authenticated",
            "iss": settings.jwt_issuer,
            "role": "authenticated",
            **overrides,
        },
        SECRET,
        algorithm="HS256",
    )


@pytest.fixture
def db(settings):
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute(
            "TRUNCATE hq.households,hq.memberships,hq.children,hq.sessions,hq.jobs,hq.proposals,hq.plans,hq.preferences,hq.child_goals,hq.events CASCADE"
        )
        conn.execute("TRUNCATE hq.catalog,hq.chapters,hq.policy")
        conn.execute(
            "TRUNCATE hq.release_items,hq.catalog_releases,hq.habit_variants,hq.habit_tags,hq.habit_prereqs,"
            "hq.habit_goals,hq.habit_levels,hq.habit_versions,hq.habits,hq.concern_goals,hq.concerns,"
            "hq.goals,hq.chapter_templates CASCADE"
        )
        conn.execute((Path(__file__).parents[1] / "supabase/seed.sql").read_text())
        conn.execute("INSERT INTO hq.policy VALUES('onboarding',1,3)")
        yield conn


@pytest.fixture
def client(settings, db):
    with TestClient(create_app(settings)) as c:
        c.headers["Authorization"] = "Bearer " + token(settings)
        yield c


def onboard(client, **overrides):
    response = client.post("/v1/children", json={"name": "Alex", "age_band": "6-9"})
    assert response.status_code == 201, response.text
    child = response.json()
    data = {
        "concerns": "Bedtime and rushed mornings; school bag and plate cleanup",
        "goals": ["bedtime", "morning", "responsibility"],
        "bedtime": "20:30",
        "morning": "07:30",
        **overrides,
    }
    response = client.post(f"/v1/children/{child['id']}/onboarding", json=data)
    assert response.status_code == 201, response.text
    return child, response.json()


def queued(client):
    child, session = onboard(client)
    response = client.post(
        f"/v1/onboarding/{session['id']}/confirm-goals",
        json={"expected_version": 1, "goals": session["data"]["goals"]},
    )
    assert response.status_code == 200, response.text
    response = client.post(f"/v1/onboarding/{session['id']}/plan-runs", headers={"Idempotency-Key": "first"})
    assert response.status_code == 202, response.text
    return child, session, response.json()["run_id"]
