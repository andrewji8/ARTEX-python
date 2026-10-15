"""API-level tests for auth flow using FastAPI TestClient (no DB required)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.main import app


@pytest.fixture(scope="module")
def client():
    # TestClient runs the app lifespan; DB is not touched by auth endpoints.
    with TestClient(app) as c:
        yield c


class TestAuthFlow:
    def test_login_json_body_returns_token(self, client):
        r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        assert r.status_code == 200
        body = r.json()
        assert "access_token" in body
        assert body["token_type"] == "bearer"
        assert body["username"] == "admin"

    def test_login_bad_credentials_401(self, client):
        r = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        assert r.status_code == 401

    def test_login_missing_fields_422(self, client):
        r = client.post("/api/auth/login", json={"username": "admin"})
        assert r.status_code == 422

    def test_me_requires_auth(self, client):
        r = client.get("/api/auth/me")
        assert r.status_code == 401

    def test_me_with_token(self, client):
        tok = client.post(
            "/api/auth/login", json={"username": "operator", "password": "operator123"}
        ).json()["access_token"]
        r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        assert r.json()["username"] == "operator"

    def test_health_public(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
