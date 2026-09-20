import requests

BASE = "http://localhost:8000"

def test_health():
    r = requests.get(f"{BASE}/api/health")
    assert r.status_code == 200, "Health failed"
    print("Health OK")

def test_guest_flow():
    # Create session
    s = requests.post(f"{BASE}/api/guest/session")
    assert s.status_code == 200
    sid = s.json()["sessionId"]
    # Extract
    data = {"command": "What is 2+2?", "workspace": "general", "sessionId": sid}
    r = requests.post(f"{BASE}/api/guest/extract", data=data)
    assert r.status_code == 200
    assert "4" in r.json()["text"] or "four" in r.json()["text"].lower()
    print("Guest flow OK")

def test_diagnose():
    r = requests.get(f"{BASE}/api/v1/diagnose")
    assert r.status_code == 200
    providers = r.json()["providers"]
    healthy = [p for p, v in providers.items() if v.get("status") == "healthy"]
    print(f"Healthy providers: {len(healthy)}/{len(providers)}")
    # At least Cloudflare and Mistral should be healthy (they were in your output)
    assert "cloudflare" in healthy and "mistral" in healthy

if __name__ == "__main__":
    test_health()
    test_guest_flow()
    test_diagnose()
    print("All E2E tests passed.")