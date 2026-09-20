# tests/test_routes.py
from fastapi.testclient import TestClient

from app import app

client = TestClient(app)

def test_health():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert "status" in response.json()

def test_guest_session():
    response = client.post("/api/guest/session")
    assert response.status_code == 200
    data = response.json()
    assert "sessionId" in data

# Add more tests for auth, extract, etc.