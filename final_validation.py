
import time
from datetime import datetime, timedelta

import jwt
import requests

# This is the same secret key used in the FastAPI application
SECRET_KEY = "a-very-strong-and-secure-secret-for-testing"
ALGORITHM = "HS256"
ADMIN_EMAIL = "shanh1346@gmail.com"

def create_admin_token():
    """Creates a JWT for the admin user."""
    payload = {
        "sub": ADMIN_EMAIL,
        "exp": datetime.utcnow() + timedelta(minutes=60)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def validate_providers():
    """Validates all AI providers."""
    print("--- Waiting for server to start ---")
    time.sleep(10)
    
    providers = [
        "gemini", "groq", "cloudflare", "openrouter", "modelscope", "ollama_cloud",
        "nara_router", "mistral", "huggingface", "github_models", "ovhcloud",
        "siliconflow", "agnes_ai", "bifrost", "freegpt4_api", "bazaarlink",
        "requesty", "nrouter", "puter", "freetheai", "omnigpt_gateway",
        "opencode_zen", "freeflow", "qoder", "manifest", "keylessai", "glama",
        "chubvenus", "blockrun", "anyapi", "aymo", "zerotwo", "aihubmix",
        "aisure", "zhipuai", "teamorouter", "proxygatellm", "free_llm_gateway",
        "ninerouter", "local"
    ]
    
    token = create_admin_token()
    headers = {"Authorization": f"Bearer {token}"}
    
    print("--- Starting Provider Validation ---")
    
    all_healthy = True
    for provider in providers:
        url = f"http://localhost:8000/api/admin/validate-provider?provider_name={provider}"
        try:
            response = requests.post(url, headers=headers)
            response.raise_for_status()
            
            data = response.json()
            if data.get("status") == "healthy":
                print(f"[✓] {provider}: Healthy (Latency: {data.get('latency_ms')}ms)")
            else:
                all_healthy = False
                print(f"[✗] {provider}: Unhealthy - {data.get('reason') or data.get('error')}")
                
        except requests.exceptions.RequestException as e:
            all_healthy = False
            print(f"[✗] {provider}: Request failed - {e}")
            
    print("\n--- Provider Validation Complete ---")
    if all_healthy:
        print("All providers are healthy.")
    else:
        print("Some providers are unhealthy.")

if __name__ == "__main__":
    validate_providers()