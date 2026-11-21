from fastapi.testclient import TestClient
from api.main import app

client = TestClient(app)

print("Testing OPTIONS /api/health...")
r = client.options(
    "/api/health",
    headers={"Origin": "https://example.com", "Access-Control-Request-Method": "GET"},
)
print(f"Status: {r.status_code}")
print(f"Headers: {dict(r.headers)}")
print(f"Content: {r.text}")
