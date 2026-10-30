"""Google sign-in, families and caregivers, the family PIN, kid sessions and CORS."""

import time
from uuid import uuid4

import jwt
import psycopg
import pytest
from conftest import OTHER, PARENT, token
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from habitquest_backend import auth
from habitquest_backend.auth import check_pin, hash_pin
from habitquest_backend.config import Settings
from habitquest_backend.db import transaction
from habitquest_backend.main import create_app

CLIENT_ID = "1234-test.apps.googleusercontent.com"
GOOGLE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
FAMILY = {
    "name": "Rivera",
    "timezone": "Europe/London",
    "pin": "1234",
    "display_name": "Sam",
    "role": "mother",
}


def google_token(**overrides):
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "google-123",
        "email": "sam@example.com",
        "email_verified": True,
        "name": "Sam Rivera",
        "iat": now,
        "exp": now + 300,
        **overrides,
    }
    claims = {key: value for key, value in claims.items() if value is not None}
    return jwt.encode(claims, GOOGLE_KEY, algorithm="RS256")


@pytest.fixture
def google(settings, db, monkeypatch):
    monkeypatch.setattr(auth, "google_key", lambda credential: GOOGLE_KEY.public_key())
    with TestClient(create_app(settings.model_copy(update={"google_client_id": CLIENT_ID}))) as c:
        yield c


def users(db, *ids):
    for user in ids:
        db.execute("INSERT INTO hq.users(id,name) VALUES(%s,'Test')", (user,))


def as_user(client, settings, user):
    return {"Authorization": "Bearer " + token(settings, user)}


@pytest.fixture
def family(client, db):
    users(db, PARENT, OTHER)
    response = client.post("/v1/families", json=FAMILY)
    assert response.status_code == 201, response.text
    return response.json()


def child(db, family_id, name="Alex"):
    return db.execute(
        "INSERT INTO hq.children(family_id,name,age) VALUES(%s,%s,7) RETURNING id", (family_id, name)
    ).fetchone()[0]


# ── Google sign-in ────────────────────────────────────────────────────────────


def test_google_sign_in_creates_then_reuses_the_user(google, db):
    first = google.post("/v1/auth/google", json={"credential": google_token()})
    assert first.status_code == 200, first.text
    assert first.json()["expires_in"] == 8 * 3600
    again = google.post(
        "/v1/auth/google", json={"credential": google_token(email="sam@new.example", name=None)}
    )
    assert db.execute("SELECT google_sub,email,name FROM hq.users").fetchall() == [
        ("google-123", "sam@new.example", "sam")  # no name in the token: the email's local part
    ]
    me = google.get("/v1/me", headers={"Authorization": "Bearer " + again.json()["access_token"]}).json()
    assert (me["name"], me["email"], me["families"]) == ("sam", "sam@new.example", [])


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "someone-else.apps.googleusercontent.com"},
        {"iss": "https://evil.example"},
        {"email_verified": False},
        {"email_verified": None},
        {"exp": int(time.time()) - 60},
    ],
)
def test_google_tokens_that_fail_a_check_are_refused(google, overrides):
    response = google.post("/v1/auth/google", json={"credential": google_token(**overrides)})
    assert response.status_code == 401


def test_a_token_signed_by_another_key_is_refused(google):
    forged = jwt.encode(
        jwt.decode(google_token(), options={"verify_signature": False}),
        rsa.generate_private_key(public_exponent=65537, key_size=2048),
        algorithm="RS256",
    )
    assert google.post("/v1/auth/google", json={"credential": forged}).status_code == 401


def test_google_sign_in_unconfigured_or_unreachable_is_503(client, google, monkeypatch):
    assert client.post("/v1/auth/google", json={"credential": google_token()}).status_code == 503

    def unreachable(credential):
        raise jwt.PyJWKClientConnectionError("network down")

    monkeypatch.setattr(auth, "google_key", unreachable)
    assert google.post("/v1/auth/google", json={"credential": google_token()}).status_code == 503


def test_google_keys_are_fetched_from_google_once_and_cached(monkeypatch):
    created = []

    class Keys:
        def __init__(self, url, **options):
            created.append((url, options))

        def get_signing_key_from_jwt(self, credential):
            return type("Key", (), {"key": "public-key"})()

    monkeypatch.setattr(auth, "_google_keys", None)
    monkeypatch.setattr(jwt, "PyJWKClient", Keys)
    assert auth.google_key("a") == auth.google_key("b") == "public-key"
    assert created == [(auth.GOOGLE_CERTS, {"cache_keys": True, "lifespan": 3600})]


# ── Families and caregivers ───────────────────────────────────────────────────


def test_creating_a_family_makes_the_caller_its_first_caregiver(client, family):
    assert (family["name"], family["timezone"], family["has_pin"]) == ("Rivera", "Europe/London", True)
    assert len(family["invite_code"]) == 8 and "pin_hash" not in family
    assert family["caregivers"] == [{"user_id": str(PARENT), "display_name": "Sam", "role": "mother"}]
    me = client.get("/v1/me").json()
    assert me["families"] == [{"id": family["id"], "name": "Rivera", "display_name": "Sam", "role": "mother"}]


@pytest.mark.parametrize(
    "change",
    [
        {"timezone": "Mars/Base"},
        {"pin": "12345"},
        {"pin": "abcd"},
        {"role": "uncle"},
        {"name": ""},
        {"extra": 1},
    ],
)
def test_family_input_is_validated(client, db, change):
    users(db, PARENT)
    assert client.post("/v1/families", json={**FAMILY, **change}).status_code == 422


def test_a_second_caregiver_joins_with_the_invite_code(client, settings, db, family):
    jo = as_user(client, settings, OTHER)
    code = family["invite_code"]
    loose = f" {code[:4].lower()}-{code[4:]} "
    joined = client.post(
        "/v1/families/join", json={"invite_code": loose, "display_name": "Jo", "role": "father"}, headers=jo
    )
    assert joined.status_code == 200, joined.text
    assert [c["role"] for c in joined.json()["caregivers"]] == ["mother", "father"]
    again = client.post(
        "/v1/families/join", json={"invite_code": code, "display_name": "Jo", "role": "father"}, headers=jo
    )
    assert again.status_code == 409
    unknown = client.post(
        "/v1/families/join",
        json={"invite_code": "ZZZZZZZZ", "display_name": "Jo", "role": "other"},
        headers=jo,
    )
    assert unknown.status_code == 404


def test_non_members_cannot_see_or_change_a_family(client, settings, family):
    outsider = as_user(client, settings, OTHER)
    base = f"/v1/families/{family['id']}"
    assert client.get(base, headers=outsider).status_code == 404
    assert client.patch(base, json={"name": "Taken"}, headers=outsider).status_code == 404
    assert client.patch(base + "/caregivers/me", json={"role": "other"}, headers=outsider).status_code == 404
    assert client.post(base + "/invite-code", headers=outsider).status_code == 404
    assert client.put(base + "/pin", json={"pin": "0000"}, headers=outsider).status_code == 404
    assert client.post(base + "/pin/verify", json={"pin": "1234"}, headers=outsider).status_code == 404
    assert client.get(base).json()["name"] == "Rivera"


def test_editing_a_family_and_my_caregiver_details(client, family):
    base = f"/v1/families/{family['id']}"
    renamed = client.patch(base, json={"name": "Rivera-Okafor"}).json()
    assert (renamed["name"], renamed["timezone"]) == ("Rivera-Okafor", "Europe/London")
    assert client.patch(base, json={"timezone": "Asia/Kolkata"}).json()["timezone"] == "Asia/Kolkata"
    assert client.patch(base, json={"timezone": "Nowhere"}).status_code == 422
    assert client.patch(base, json={"timezone": None}).json()["timezone"] == "Asia/Kolkata"  # null: unchanged
    me = client.patch(base + "/caregivers/me", json={"display_name": "Mum"}).json()["caregivers"][0]
    assert (me["display_name"], me["role"]) == ("Mum", "mother")
    assert (
        client.patch(base + "/caregivers/me", json={"role": "guardian"}).json()["caregivers"][0]["role"]
        == "guardian"
    )


def test_a_new_invite_code_replaces_the_old_one(client, settings, family):
    fresh = client.post(f"/v1/families/{family['id']}/invite-code").json()["invite_code"]
    assert fresh != family["invite_code"]
    jo = as_user(client, settings, OTHER)
    join = {"display_name": "Jo", "role": "father"}
    assert (
        client.post(
            "/v1/families/join", json={**join, "invite_code": family["invite_code"]}, headers=jo
        ).status_code
        == 404
    )
    assert (
        client.post("/v1/families/join", json={**join, "invite_code": fresh}, headers=jo).status_code == 200
    )


def test_the_pin_is_checked_and_can_be_changed(client, family):
    base = f"/v1/families/{family['id']}/pin"
    assert client.post(base + "/verify", json={"pin": "1234"}).json() == {"valid": True}
    assert client.post(base + "/verify", json={"pin": "4321"}).json() == {"valid": False}
    assert client.put(base, json={"pin": "4321"}).status_code == 204
    assert client.post(base + "/verify", json={"pin": "1234"}).json() == {"valid": False}
    assert client.post(base + "/verify", json={"pin": "4321"}).json() == {"valid": True}


def test_pin_hashes_are_salted():
    first, second = hash_pin("1234"), hash_pin("1234")
    assert first != second and check_pin("1234", first) and check_pin("1234", second)
    assert not check_pin("1234", None)


def test_family_functions_need_a_signed_in_user(settings, db):
    with transaction(settings) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("SELECT hq.create_family('x','UTC',NULL,'CODE1234','Sam','mother')")
    with transaction(settings) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("SELECT hq.join_family('CODE1234','Sam','mother')")


# ── Kid mode ──────────────────────────────────────────────────────────────────


def test_kid_session_gives_a_token_only_for_kid_endpoints(client, settings, db, family):
    alex = child(db, family["id"])
    session = client.post(f"/v1/children/{alex}/kid-session")
    assert session.status_code == 200, session.text
    body = session.json()
    assert body["child"] == {"id": str(alex), "name": "Alex"} and body["expires_in"] == 8 * 3600
    kid = {"Authorization": "Bearer " + body["access_token"]}
    me = client.get("/v1/kid/me", headers=kid).json()
    assert (me["name"], me["age"], me["buddy"], me["world"]) == ("Alex", 7, "fox", "space")
    assert client.get("/v1/me", headers=kid).status_code == 401
    assert client.get(f"/v1/families/{family['id']}", headers=kid).status_code == 401
    assert client.get("/v1/kid/me").status_code == 401  # a caregiver token is not a kid token


def test_kid_sessions_only_for_the_callers_children(client, settings, db, family):
    stranger = db.execute("INSERT INTO hq.families(name) VALUES('Okafor') RETURNING id").fetchone()[0]
    assert client.post(f"/v1/children/{child(db, stranger)}/kid-session").status_code == 404
    assert client.post(f"/v1/children/{uuid4()}/kid-session").status_code == 404


def test_a_kid_sees_only_their_own_child_row(client, settings, db, family):
    alex, maya = child(db, family["id"]), child(db, family["id"], "Maya")
    with transaction(settings, kid=alex) as conn:
        assert conn.execute("SELECT id FROM hq.children").fetchall() == [{"id": alex}]
    for table in ("families", "caregivers", "users", "pain_points", "child_goals"):
        with transaction(settings, kid=alex) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(f"SELECT 1 FROM hq.{table}")
    db.execute("DELETE FROM hq.children WHERE id=%s", (maya,))
    with TestClient(create_app(settings)) as kid_client:
        kid = {"Authorization": "Bearer " + token(settings, maya, role="kid")}
        assert kid_client.get("/v1/kid/me", headers=kid).status_code == 404


# ── Browser access ────────────────────────────────────────────────────────────


def test_cors_allows_only_the_configured_origins(client):
    preflight = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization"}
    allowed = client.options("/v1/families", headers={"Origin": "http://localhost:5180", **preflight})
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5180"
    refused = client.options("/v1/families", headers={"Origin": "https://evil.example", **preflight})
    assert "access-control-allow-origin" not in refused.headers


def test_origins_are_read_from_a_comma_separated_list():
    assert Settings(cors_origins="https://app.example, http://localhost:5180,").allowed_origins == [
        "https://app.example",
        "http://localhost:5180",
    ]
