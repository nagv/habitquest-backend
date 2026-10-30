from conftest import OTHER, token
from fastapi.testclient import TestClient

from habitquest_backend.main import create_app


def test_health(client, settings):
    assert client.get("/health/live").json() == {"status": "ok"}
    assert client.get("/health/ready").json() == {"status": "ready"}
    broken = settings.model_copy(update={"database_url": "postgresql://nobody@127.0.0.1:1/missing_test"})
    assert TestClient(create_app(broken)).get("/health/ready").status_code == 503


def test_goals_are_the_active_catalog_goals(client, db):
    goals = client.get("/v1/goals").json()
    assert [g["id"] for g in goals] == [
        "bedtime",
        "feelings",
        "meals",
        "screens",
        "responsibility",
        "homework",
        "mornings",
    ]
    assert set(goals[0]) == {"id", "title", "description"}
    db.execute("UPDATE hq.goals SET is_active=false WHERE id='screens'")
    assert "screens" not in [g["id"] for g in client.get("/v1/goals").json()]


def test_habits_for_a_goal_filter_by_age_and_tier(client, db):
    habits = client.get("/v1/goals/mornings/habits").json()
    assert [h["id"] for h in habits] == ["dress-self", "wake", "make-bed", "pack-bag", "plan-tomorrow"]
    assert habits[0]["kid_title"] == "Dress-up champ" and habits[0]["points"] == 10
    nine = client.get("/v1/goals/mornings/habits", params={"age": 9}).json()
    assert [h["id"] for h in nine] == ["wake", "make-bed", "pack-bag", "plan-tomorrow"]
    starters = client.get("/v1/goals/mornings/habits", params={"age": 9, "max_tier": 1}).json()
    assert [h["id"] for h in starters] == ["wake"]
    db.execute("UPDATE hq.habits SET is_active=false WHERE id='wake'")
    assert client.get("/v1/goals/mornings/habits", params={"age": 9, "max_tier": 1}).json() == []


def test_unknown_or_withdrawn_goal_is_404(client, db):
    assert client.get("/v1/goals/flying/habits").status_code == 404
    db.execute("UPDATE hq.goals SET is_active=false WHERE id='meals'")
    assert client.get("/v1/goals/meals/habits").status_code == 404


def test_catalog_query_limits(client):
    for params in ({"age": 2}, {"age": 14}, {"max_tier": 0}, {"max_tier": 4}):
        assert client.get("/v1/goals/meals/habits", params=params).status_code == 422


def test_tokens_are_required_and_checked(client, settings):
    assert client.get("/v1/goals", headers={"Authorization": ""}).status_code == 401
    bad = {
        "expired": token(settings, exp=0),
        "wrong role": token(settings, role="anon"),
        "wrong issuer": token(settings, iss="http://elsewhere"),
        "not a uuid": token(settings, sub="parent"),
        "garbage": "not-a-jwt",
    }
    for case, value in bad.items():
        response = client.get("/v1/goals", headers={"Authorization": "Bearer " + value})
        assert response.status_code == 401, case
    assert client.get("/v1/goals", headers={"Authorization": "Bearer " + token(settings, OTHER)}).is_success


def test_tokens_are_refused_without_a_signing_secret(settings, db):
    with TestClient(create_app(settings.model_copy(update={"jwt_secret": None}))) as unsigned:
        response = unsigned.get("/v1/goals", headers={"Authorization": "Bearer " + token(settings)})
        assert response.status_code == 401


def test_every_endpoint_is_documented(settings):
    """/docs shows a purpose for every operation, grouped under a described tag."""
    spec = create_app(
        settings.model_copy(update={"dev_login": True, "dev_login_password": "x" * 12})
    ).openapi()
    tags = {tag["name"] for tag in spec["tags"]}
    operations = [(method, path, op) for path, ops in spec["paths"].items() for method, op in ops.items()]
    assert len(operations) == 17
    undocumented = [
        (method, path)
        for method, path, op in operations
        if not op.get("summary") or len(op.get("description", "")) < 40 or not set(op.get("tags", [])) <= tags
    ]
    assert undocumented == []
