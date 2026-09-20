
import asyncio
import os
import subprocess
import time
import uuid

import httpx

# --- Configuration ---
HOST = "127.0.0.1"
PORT = 8000
BASE_URL = f"http://{HOST}:{PORT}"
API_URL = f"{BASE_URL}/api/chat"
UPLOAD_URL = f"{BASE_URL}/api/upload-for-chat" # Assuming this is the endpoint
VENV_PYTHON = r"c:\Users\alime\Downloads\AXELR AI\Axelr-backend\.venv-test\Scripts\python.exe"
UVICORN_PATH = r"c:\Users\alime\Downloads\AXELR AI\Axelr-backend\.venv-test\Scripts\uvicorn.exe"
APP_PATH = "app:app"
APP_DIR = r"c:\Users\alime\Downloads\AXELR AI\Axelr-backend"

# --- ANSI Color Codes ---
class Colors:
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BLUE = '\033[94m'
    RESET = '\033[0m'

def print_color(color, message):
    print(f"{color}{message}{Colors.RESET}")

# --- Provider Lists (extracted from app.py) ---
# This is a simplified representation. The script will test these.
KEY_BASED_PROVIDERS = [
    "groq", "cloudflare", "openrouter", "huggingface", "gemini", "mistral",
    "github", "nrouter", "text-cortex", "nararouter", "bazaarlink",
    "siliconflow", "agnes", "ollama", "anyapi", "modelscope", "ovhcloud",
    "requesty", "manifest", "glama", "zai", "teamorouter"
]

STATIC_PROVIDERS = {
    "bifrost": ["llama3.1:70b"],
    "freegpt4": ["gpt-4"],
    "puter": ["gpt-3.5-turbo"],
    "freetheai": ["gpt-3.5-turbo"],
    "omnigpt": ["gpt-3.5-turbo"],
    "opendode": ["qwen3-coder"],
    "freeflow": ["gpt-3.5-turbo"],
    "qoder": ["qwen3-coder"],
    "manifest": ["auto:free"],
    "keyless": ["gpt-3.5-turbo"],
    "chubvenus": ["gpt-3.5-turbo"],
    "blockrun": ["deepseek-v4-flash"],
    "aymo": ["gemini-flash"],
    "zerotwo": ["gpt-5-mini"],
    "aihubmix": ["gpt-5.5"],
    "aisure": ["gpt-4o"]
}

async def wait_for_server(url: str, timeout: int = 60):
    """Wait for the server to be available."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, timeout=5)
                if response.status_code == 200 or response.status_code == 404: # 404 means routing is working
                    print_color(Colors.GREEN, "✅ Server is up!")
                    return True
        except httpx.ConnectError:
            await asyncio.sleep(1)
        except Exception as e:
            print_color(Colors.YELLOW, f"Server check failed with error: {e}")
            await asyncio.sleep(1)
    return False

async def test_provider(client: httpx.AsyncClient, provider: str, model: str):
    """Tests a single provider and model."""
    print(f"  Testing model: {Colors.BLUE}{model}{Colors.RESET} via provider: {Colors.BLUE}{provider}{Colors.RESET}...")
    payload = {
        "messages": [{"role": "user", "content": "Hello"}],
        "provider": provider,
        "model": model,
        "workspaceId": "validation_test",
        "stream": False
    }
    try:
        response = await client.post(API_URL, json=payload, timeout=30)
        if 200 <= response.status_code < 300:
            print_color(Colors.GREEN, f"    ✅ SUCCESS: {provider}/{model} responded with {response.status_code}")
            return True
        else:
            error_info = response.text[:100]
            print_color(Colors.RED, f"    ❌ FAILED: {provider}/{model} responded with {response.status_code}. Info: {error_info}")
            return False
    except httpx.ReadTimeout:
        print_color(Colors.RED, f"    ❌ FAILED: {provider}/{model} timed out.")
        return False
    except Exception as e:
        print_color(Colors.RED, f"    ❌ FAILED: {provider}/{model} raised an exception: {e}")
        return False

async def test_semantic_cache(client: httpx.AsyncClient):
    """Tests the semantic cache functionality."""
    print_color(Colors.BLUE, "\n--- Testing Semantic Cache ---")
    # Use a provider likely to be configured and fast, like groq
    provider, model = "groq", "llama3-70b-8192"
    
    payload = {
        "messages": [{"role": "user", "content": f"What is the capital of France? Answer concisely. UUID: {uuid.uuid4()}"}],
        "provider": provider,
        "model": model,
        "workspaceId": "cache_test",
        "stream": False
    }

    # First call (should be a cache miss)
    print("  Sending first request (cache miss)...")
    start_miss = time.time()
    await client.post(API_URL, json=payload, timeout=30)
    duration_miss = time.time() - start_miss
    print(f"  First request took {duration_miss:.2f}s")

    # Second call (should be a cache hit)
    print("  Sending second request (should be a cache hit)...")
    start_hit = time.time()
    await client.post(API_URL, json=payload, timeout=30)
    duration_hit = time.time() - start_hit
    print(f"  Second request took {duration_hit:.2f}s")

    if duration_hit < duration_miss and duration_hit < 1.0:
        print_color(Colors.GREEN, f"    ✅ SUCCESS: Cache hit confirmed (Hit: {duration_hit:.2f}s < Miss: {duration_miss:.2f}s)")
        return True
    else:
        print_color(Colors.RED, f"    ❌ FAILED: Cache behavior not detected (Hit: {duration_hit:.2f}s, Miss: {duration_miss:.2f}s)")
        return False

async def test_file_upload(client: httpx.AsyncClient):
    """Tests file upload functionality (Supabase)."""
    print_color(Colors.BLUE, "\n--- Testing File Upload (Supabase) ---")
    try:
        # Create a dummy file in memory
        file_content = b"This is a test file for Supabase."
        files = {'file': ('test.txt', file_content, 'text/plain')}
        
        # The API expects a workspaceId and other data
        data = {'workspaceId': 'upload_test'}

        response = await client.post(UPLOAD_URL, files=files, data=data, timeout=30)

        if 200 <= response.status_code < 300:
            response_json = response.json()
            print_color(Colors.GREEN, f"    ✅ SUCCESS: File uploaded successfully. Response: {response_json}")
            return True
        else:
            error_info = response.text[:100]
            print_color(Colors.RED, f"    ❌ FAILED: File upload returned status {response.status_code}. Info: {error_info}")
            return False
    except Exception as e:
        print_color(Colors.RED, f"    ❌ FAILED: File upload raised an exception: {e}")
        return False

async def main():
    server_process = None
    try:
        # Start the FastAPI server
        print_color(Colors.BLUE, "--- Starting FastAPI Server ---")
        server_process = subprocess.Popen(
            [UVICORN_PATH, APP_PATH, "--host", HOST, "--port", str(PORT), "--reload"],
            cwd=APP_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        if not await wait_for_server(BASE_URL):
            print_color(Colors.RED, "Server failed to start. Aborting tests.")
            stdout, stderr = server_process.communicate()
            print("--- Server STDOUT ---")
            print(stdout.decode(errors='ignore'))
            print("--- Server STDERR ---")
            print(stderr.decode(errors='ignore'))
            return

        async with httpx.AsyncClient() as client:
            # --- Test Key-Based Providers ---
            print_color(Colors.BLUE, "\n--- Testing Key-Based Providers (will skip if key is missing) ---")
            for provider in KEY_BASED_PROVIDERS:
                # A simple check to see if the env var might exist
                if os.getenv(f"{provider.upper()}_API_KEY") or provider in ["cloudflare", "github"]:
                    print(f"\nTesting provider: {Colors.YELLOW}{provider}{Colors.RESET}")
                    # This is a simplification; we test a common model.
                    # A real implementation would need to know the exact models for each provider.
                    model = "auto" # Most routers support "auto"
                    if provider == "groq": model = "llama3-70b-8192"
                    if provider == "cloudflare": model = "@cf/meta/llama-3.1-8b-instruct"
                    
                    await test_provider(client, provider, model)
                else:
                    print(f"\nSkipping provider: {Colors.YELLOW}{provider}{Colors.RESET} (API key likely not set)")

            # --- Test Static/Proxy Providers ---
            print_color(Colors.BLUE, "\n--- Testing Static & Proxy Providers ---")
            for provider, models in STATIC_PROVIDERS.items():
                print(f"\nTesting provider: {Colors.YELLOW}{provider}{Colors.RESET}")
                for model in models:
                    await test_provider(client, provider, model)
            
            # --- Test Core Functionality ---
            await test_semantic_cache(client)
            # await test_file_upload(client) # Disabling for now to focus on AI providers

    finally:
        if server_process:
            print_color(Colors.BLUE, "\n--- Shutting down server ---")
            server_process.terminate()
            await asyncio.sleep(2) # Give it time to shut down
            server_process.kill()
            print("Server stopped.")

if __name__ == "__main__":
    asyncio.run(main())