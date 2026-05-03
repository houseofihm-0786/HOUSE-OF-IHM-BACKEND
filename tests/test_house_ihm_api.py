"""Backend API tests for House of IHM EdTech API.

Covers:
- GET /api/ root message
- POST/GET /api/contact (validation + persistence + no _id leakage)
- POST/GET /api/status backward-compat
"""
import os
import re
import uuid

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL")
if not BASE_URL:
    # Fallback: read from /app/frontend/.env
    try:
        with open("/app/frontend/.env") as f:
            for line in f:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    BASE_URL = line.split("=", 1)[1].strip()
                    break
    except Exception:
        pass

assert BASE_URL, "REACT_APP_BACKEND_URL must be set"
BASE_URL = BASE_URL.rstrip("/")
API = f"{BASE_URL}/api"


@pytest.fixture(scope="module")
def client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


# ---------- Root ----------
class TestRoot:
    def test_root_message(self, client):
        r = client.get(f"{API}/")
        assert r.status_code == 200
        assert r.json() == {"message": "House of IHM EdTech API"}


# ---------- Contact ----------
class TestContact:
    def test_create_contact_valid_persists_and_returns_clean(self, client):
        unique = f"TEST_{uuid.uuid4().hex[:8]}"
        payload = {
            "name": f"TEST User {unique}",
            "email": f"{unique}@example.com",
            "message": f"Hello from automated test {unique}",
        }
        r = client.post(f"{API}/contact", json=payload)
        assert r.status_code == 200, r.text
        data = r.json()

        # data validation
        assert data["name"] == payload["name"]
        assert data["email"] == payload["email"]
        assert data["message"] == payload["message"]
        assert "id" in data and isinstance(data["id"], str) and len(data["id"]) > 0
        assert "created_at" in data
        assert "_id" not in data, "Mongo _id must not leak in API response"

        # GET to verify persistence
        r2 = client.get(f"{API}/contact")
        assert r2.status_code == 200
        items = r2.json()
        assert isinstance(items, list)
        match = [c for c in items if c.get("id") == data["id"]]
        assert len(match) == 1, "Newly created contact not found in GET list"
        fetched = match[0]
        assert fetched["email"] == payload["email"]
        assert fetched["name"] == payload["name"]
        assert "_id" not in fetched

    def test_get_contacts_no_objectid_leak(self, client):
        r = client.get(f"{API}/contact")
        assert r.status_code == 200
        items = r.json()
        assert isinstance(items, list)
        for c in items:
            assert "_id" not in c
            assert "id" in c
            assert "email" in c
            assert "name" in c
            assert "message" in c
            assert "created_at" in c

    def test_create_contact_invalid_email_returns_422(self, client):
        r = client.post(
            f"{API}/contact",
            json={"name": "Bob", "email": "not-an-email", "message": "hi"},
        )
        assert r.status_code == 422

    def test_create_contact_empty_fields_returns_422(self, client):
        r = client.post(
            f"{API}/contact",
            json={"name": "", "email": "", "message": ""},
        )
        assert r.status_code == 422

    def test_create_contact_missing_fields_returns_422(self, client):
        r = client.post(f"{API}/contact", json={})
        assert r.status_code == 422


# ---------- Status (backward compat) ----------
class TestStatus:
    def test_create_and_list_status(self, client):
        client_name = f"TEST_status_{uuid.uuid4().hex[:6]}"
        r = client.post(f"{API}/status", json={"client_name": client_name})
        assert r.status_code == 200, r.text
        created = r.json()
        assert created["client_name"] == client_name
        assert "id" in created and "timestamp" in created
        assert "_id" not in created

        r2 = client.get(f"{API}/status")
        assert r2.status_code == 200
        items = r2.json()
        assert isinstance(items, list)
        assert any(s.get("client_name") == client_name for s in items)
        for s in items:
            assert "_id" not in s
