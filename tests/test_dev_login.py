import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from habitquest_backend.auth import PARENT, dev_identity, dev_login, issue_token, verify_token
from habitquest_backend.config import Settings
from habitquest_backend.main import create_app

PASSWORD = "correct horse battery"


@pytest.fixture
def login_settings(settings):
    return settings.model_copy(update={"dev_login": True, "dev_login_password": SecretStr(PASSWORD)})


@pytest.fixture
def login_client(login_settings, db):
    with TestClient(create_app(login_settings)) as c:
        yield c


def login(client, username, password=PASSWORD):
    return client.post("/v1/auth/token", data={"username": username, "password": password})


def test_a_development_parent_gets_a_working_token_and_a_user_record(login_client, login_settings, db):
    for _ in range(2):  # logging in again keeps the same user
        response = login(login_client, "parent")
        assert response.status_code == 200
    body = response.json()
    assert (body["token_type"], body["expires_in"]) == ("bearer", 3600)
    assert verify_token(body["access_token"], login_settings) == PARENT
    headers = {"Authorization": "Bearer " + body["access_token"]}
    assert login_client.get("/v1/goals", headers=headers).status_code == 200
    assert db.execute("SELECT id, name FROM hq.users").fetchall() == [(PARENT, "parent")]


@pytest.mark.parametrize(
    ("username", "password"),
    [("parent", "wrong password!"), ("editor", PASSWORD), ("parent:", PASSWORD), ("parent: ", PASSWORD)],
)
def test_wrong_credentials_are_refused(login_client, username, password):
    response = login(login_client, username, password)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_labelled_parents_are_stable_and_separate():
    sam = dev_identity("parent:sam")
    assert dev_identity("parent:sam") == sam
    assert sam not in (PARENT, dev_identity("parent:kim"))
    assert dev_identity("parent") == PARENT and dev_identity("guest") is None


def test_no_password_configured_refuses_every_login(settings):
    with pytest.raises(HTTPException) as refused:
        dev_login(settings, "parent", "")
    assert refused.value.status_code == 401


def test_issuing_needs_a_secret(settings):
    with pytest.raises(ValueError, match="JWT_SECRET is required"):
        issue_token(settings.model_copy(update={"jwt_secret": None}), PARENT)


def test_login_exists_only_when_enabled(client, login_client):
    assert client.post("/v1/auth/token", data={"username": "parent", "password": PASSWORD}).status_code == 404
    schemes = client.get("/openapi.json").json()["components"]["securitySchemes"]
    assert list(schemes) == ["HTTPBearer"]
    schemes = login_client.get("/openapi.json").json()["components"]["securitySchemes"]
    assert schemes["OAuth2PasswordBearer"]["flows"]["password"]["tokenUrl"] == "/v1/auth/token"
    assert "HTTPBearer" in schemes
    assert login_client.get("/v1/goals").status_code == 401


@pytest.mark.parametrize(
    "overrides",
    [
        {"dev_login": True},
        {"dev_login": True, "dev_login_password": "short"},
        {"dev_login": True, "dev_login_password": PASSWORD, "jwt_secret": None},
    ],
)
def test_dev_login_requires_a_secret_and_a_long_password(settings, overrides):
    values = {"database_url": settings.database_url, "jwt_secret": "s" * 32, **overrides}
    with pytest.raises(ValidationError, match="DEV_LOGIN requires"):
        Settings(**values)


def test_a_complete_dev_login_configuration_is_accepted(settings):
    configured = Settings(
        database_url=settings.database_url, jwt_secret="s" * 32, dev_login=True, dev_login_password=PASSWORD
    )
    assert configured.dev_login
