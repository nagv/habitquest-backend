import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from habitquest_backend.auth import EDITOR, PARENT, REVIEWER, dev_identity, dev_login, verify_token
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


def test_each_development_user_gets_a_working_token(login_client, login_settings):
    tokens = {}
    for name in ("parent", "editor", "reviewer"):
        response = login(login_client, name)
        assert response.status_code == 200
        body = response.json()
        assert (body["token_type"], body["expires_in"]) == ("bearer", 3600)
        tokens[name] = {"Authorization": "Bearer " + body["access_token"]}
    assert verify_token(tokens["parent"]["Authorization"][7:], login_settings) == PARENT
    assert login_client.get("/v1/editor/releases", headers=tokens["editor"]).status_code == 200
    assert login_client.get("/v1/editor/releases", headers=tokens["reviewer"]).status_code == 200
    assert login_client.get("/v1/editor/releases", headers=tokens["parent"]).status_code == 403
    child = login_client.post(
        "/v1/children",
        json={"name": "Alex", "age_band": "6-9", "timezone": "UTC"},
        headers=tokens["parent"],
    )
    assert child.status_code == 201
    other = {"Authorization": "Bearer " + login(login_client, "parent:other").json()["access_token"]}
    assert login_client.get(f"/v1/children/{child.json()['id']}", headers=other).status_code == 404


@pytest.mark.parametrize(
    ("username", "password"),
    [("parent", "wrong password!"), ("admin", PASSWORD), ("parent:", PASSWORD), ("parent: ", PASSWORD)],
)
def test_wrong_credentials_are_refused(login_client, username, password):
    response = login(login_client, username, password)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_labelled_parents_are_stable_and_separate():
    assert dev_identity("editor") == (EDITOR, True) and dev_identity("reviewer") == (REVIEWER, True)
    first, editor = dev_identity("parent:sam")
    assert dev_identity("parent:sam") == (first, False) and not editor
    assert first not in (PARENT, dev_identity("parent:kim")[0])


def test_no_password_configured_refuses_every_login(settings):
    with pytest.raises(HTTPException) as refused:
        dev_login(settings, "parent", "")
    assert refused.value.status_code == 401


def test_login_exists_only_when_enabled(client, login_client):
    assert client.post("/v1/auth/token", data={"username": "parent", "password": PASSWORD}).status_code == 404
    schemes = client.get("/openapi.json").json()["components"]["securitySchemes"]
    assert list(schemes) == ["HTTPBearer"]
    schemes = login_client.get("/openapi.json").json()["components"]["securitySchemes"]
    assert schemes["OAuth2PasswordBearer"]["flows"]["password"]["tokenUrl"] == "/v1/auth/token"
    assert "HTTPBearer" in schemes
    assert login_client.get("/v1/editor/releases").status_code == 401


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
