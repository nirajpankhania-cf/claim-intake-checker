"""Smoke test: the app starts and the root route responds."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_root_returns_ok() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.text == "Claim Intake Check — OK"
