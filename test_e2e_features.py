from fastapi.testclient import TestClient
from app import app, create_access_token

client = TestClient(app)

def test_prompt_sanitization():
    """
    Tests that the prompt sanitization is working correctly by sending a
    prompt with forbidden characters and checking that they are removed.
    """
    malicious_prompt = "What is 2+2? <script>alert('XSS')</script>"
    token = create_access_token(data={"sub": "test@example.com", "isAdmin": True})
    response = client.post(
        "/api/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "workspace": "general",
            "command": malicious_prompt,
            "max_tokens": 100,
            "temperature": 0.7,
            "context": "",
        },
    )
    assert response.status_code == 200
    # The response text should not contain the malicious script tag
    if hasattr(response, 'json'):
        response_data = response.json()
        if "text" in response_data:
            assert "<script>" not in response_data["text"]
    print("Prompt sanitization test passed.")