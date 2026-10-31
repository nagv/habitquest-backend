import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx
import jwt
import psycopg
import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict
from pydantic import SecretStr

from habitquest_backend import llm
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
    return Settings(database_url=URL, jwt_secret=SECRET, jwt_issuer="http://local-test")


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
    """An administrative connection to a clean test database with the seeded catalog."""
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute("TRUNCATE hq.users,hq.families CASCADE")
        conn.execute((Path(__file__).parents[1] / "db/seed.sql").read_text())
        yield conn


@pytest.fixture
def client(settings, db):
    with TestClient(create_app(settings)) as c:
        c.headers["Authorization"] = "Bearer " + token(settings)
        yield c


@pytest.fixture
def remote(settings):
    """Settings for the real model path; pair with `llm_reply` so nothing reaches the network."""
    return settings.model_copy(
        update={
            "ai_mode": "openai",
            "openai_base_url": "https://llm.example/v1/",
            "openai_model": "vendor/model",
            "openai_small_model": "vendor/small",
            "openai_api_key": SecretStr("k"),
        }
    )


@pytest.fixture
def llm_reply(monkeypatch):
    """Routes model calls to the returned function's handler; `.seen` holds each request's JSON body."""

    def route(*handlers):
        queue = list(handlers)

        def respond(request):
            route.seen.append(json.loads(request.content))
            reply = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(reply, httpx.Response):
                return reply
            message = {"role": "assistant", **(reply if isinstance(reply, dict) else {"content": reply})}
            return httpx.Response(
                200,
                json={
                    "id": "x",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "m",
                    "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
                },
            )

        monkeypatch.setattr(
            llm, "http_client", lambda settings: httpx.Client(transport=httpx.MockTransport(respond))
        )

    route.seen = []
    return route
