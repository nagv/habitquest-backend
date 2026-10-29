import importlib

import pytest
from fastapi.testclient import TestClient


def test_application_exposes_liveness_without_a_model_or_database():
    try:
        module = importlib.import_module("habitquest_backend.main")
    except ModuleNotFoundError:
        pytest.fail("The backend application factory has not been implemented")
    with TestClient(module.create_app()) as client:
        response = client.get("/health/live")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


def test_invalid_model_mode_is_rejected():
    try:
        module = importlib.import_module("habitquest_backend.config")
    except ModuleNotFoundError:
        pytest.fail("Validated backend configuration has not been implemented")
    with pytest.raises(ValueError):
        module.Settings(model_mode="unknown")
