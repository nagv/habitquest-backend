from fastapi.testclient import TestClient

from habitquest_backend.main import create_app


def test_onboarding_routes_require_an_authenticated_parent():
    with TestClient(create_app()) as client:
        response = client.post("/v1/children", json={"name": "Alex", "age_band": "6-9"})
        assert response.status_code == 401


def test_readiness_detects_an_unavailable_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody:nothing@127.0.0.1:1/missing?connect_timeout=1")
    with TestClient(create_app()) as client:
        assert client.get("/health/ready").status_code == 503
