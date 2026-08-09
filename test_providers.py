#!/usr/bin/env python3
"""
Test each AI provider individually using requests.
Run: python test_providers.py
"""

import os
import json
import requests
import ssl
from dotenv import load_dotenv

# Disable SSL verification for Hugging Face
ssl._create_default_https_context = ssl._create_unverified_context

load_dotenv(override=True)

# ---- Load keys ----
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
TOGETHER_API_KEY = os.getenv("TOGETHER_AI_API_KEY") or os.getenv("TOGETHER_API_KEY") or ""
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "").strip()
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
CLOUDFLARE_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY", "").strip()
HF_API_KEY = os.getenv("HUGGINGFACE_API_KEY", "").strip()

TEST_PROMPT = "Say exactly 'OK' in one word."

def test_groq():
    if not GROQ_API_KEY:
        return "SKIPPED (no key)"
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": TEST_PROMPT}],
        "max_tokens": 5,
        "temperature": 0.0
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"].strip()
        return f"ERROR {r.status_code}: {r.text[:100]}"
    except Exception as e:
        return f"EXCEPTION: {e}"

def test_openrouter():
    if not OPENROUTER_API_KEY:
        return "SKIPPED (no key)"
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://axelr.in",
        "X-Title": "Axelr AI"
    }
    payload = {
        "model": "mistralai/mistral-7b-instruct:free",   # changed to working free model
        "messages": [{"role": "user", "content": TEST_PROMPT}],
        "max_tokens": 5,
        "temperature": 0.0
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"].strip()
        return f"ERROR {r.status_code}: {r.text[:100]}"
    except Exception as e:
        return f"EXCEPTION: {e}"

def test_together():
    if not TOGETHER_API_KEY:
        return "SKIPPED (no key)"
    url = "https://api.together.xyz/v1/chat/completions"
    headers = {"Authorization": f"Bearer {TOGETHER_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "google/gemma-2-9b-it",   # changed to free model
        "messages": [{"role": "user", "content": TEST_PROMPT}],
        "max_tokens": 5,
        "temperature": 0.0
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"].strip()
        return f"ERROR {r.status_code}: {r.text[:100]}"
    except Exception as e:
        return f"EXCEPTION: {e}"

def test_mistral():
    if not MISTRAL_API_KEY:
        return "SKIPPED (no key)"
    url = "https://api.mistral.ai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {MISTRAL_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "open-mistral-7b",
        "messages": [{"role": "user", "content": TEST_PROMPT}],
        "max_tokens": 5,
        "temperature": 0.0
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"].strip()
        return f"ERROR {r.status_code}: {r.text[:100]}"
    except Exception as e:
        return f"EXCEPTION: {e}"

def test_deepseek():
    if not DEEPSEEK_API_KEY:
        return "SKIPPED (no key)"
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": TEST_PROMPT}],
        "max_tokens": 5,
        "temperature": 0.0
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"].strip()
        return f"ERROR {r.status_code}: {r.text[:100]}"
    except Exception as e:
        return f"EXCEPTION: {e}"

def test_cloudflare():
    if not CLOUDFLARE_API_TOKEN or not CLOUDFLARE_ACCOUNT_ID:
        return "SKIPPED (missing credentials)"
    url = f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/run/@cf/meta/llama-3.1-8b-instruct"
    headers = {"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messages": [{"role": "user", "content": TEST_PROMPT}],
        "max_tokens": 5,
        "temperature": 0.0
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        if r.status_code == 200:
            return r.json().get("result", {}).get("response", "").strip()
        return f"ERROR {r.status_code}: {r.text[:100]}"
    except Exception as e:
        return f"EXCEPTION: {e}"

def test_cerebras():
    if not CEREBRAS_API_KEY:
        return "SKIPPED (no key)"
    url = "https://api.cerebras.ai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {CEREBRAS_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "llama3.1-8b",
        "messages": [{"role": "user", "content": TEST_PROMPT}],
        "max_tokens": 5,
        "temperature": 0.0
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10, verify=False)
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"].strip()
        return f"ERROR {r.status_code}: {r.text[:100]}"
    except Exception as e:
        return f"EXCEPTION: {e}"

def test_huggingface():
    if not HF_API_KEY:
        return "SKIPPED (no key)"
    model = "google/gemma-2-9b-it"
    url = f"https://api-inference.huggingface.co/models/{model}"
    headers = {"Authorization": f"Bearer {HF_API_KEY}"}
    payload = {
        "inputs": TEST_PROMPT,
        "parameters": {
            "max_new_tokens": 5,
            "temperature": 0.0,
            "return_full_text": False
        }
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10, verify=False)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                return data[0].get("generated_text", "").strip()
            return data.get("generated_text", "").strip()
        return f"ERROR {r.status_code}: {r.text[:100]}"
    except Exception as e:
        return f"EXCEPTION: {e}"

def test_pollinations():
    import urllib.parse
    encoded = urllib.parse.quote(TEST_PROMPT)
    url = f"https://text.pollinations.ai/{encoded}?seed=42&model=openai"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            try:
                data = r.json()
                if isinstance(data, dict) and "text" in data:
                    return data["text"].strip()
                elif isinstance(data, str):
                    return data.strip()
                else:
                    return r.text.strip()
            except:
                return r.text.strip()
        return f"ERROR {r.status_code}: {r.text[:100]}"
    except Exception as e:
        return f"EXCEPTION: {e}"

def main():
    print("\n=== TESTING AI PROVIDERS ===\n")
    tests = [
        ("Groq", test_groq),
        ("OpenRouter", test_openrouter),
        ("Together AI", test_together),
        ("Mistral", test_mistral),
        ("DeepSeek", test_deepseek),
        ("Cloudflare", test_cloudflare),
        ("Cerebras", test_cerebras),
        ("Hugging Face", test_huggingface),
        ("Pollinations", test_pollinations),
    ]
    results = {}
    for name, func in tests:
        print(f"Testing {name}... ", end="", flush=True)
        result = func()
        results[name] = result
        print("OK" if "OK" in result else result[:60])
    print("\n=== SUMMARY ===")
    for name, res in results.items():
        status = "✅" if "OK" in res else ("⚠️" if "SKIPPED" in res else "❌")
        print(f"{status} {name}: {res[:80]}")

if __name__ == "__main__":
    main()

    #!/usr/bin/env python3
"""
Test each AI provider individually.
Run: python test_providers.py
"""
import os, json, requests, ssl
from dotenv import load_dotenv
ssl._create_default_https_context = ssl._create_unverified_context
load_dotenv(override=True)

# Keys
CLOUDFLARE_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
HF_API_KEY = os.getenv("HUGGINGFACE_API_KEY", "").strip()
TEST_PROMPT = "Say exactly 'OK' in one word."

def test_cloudflare():
    if not CLOUDFLARE_API_TOKEN or not CLOUDFLARE_ACCOUNT_ID:
        return "SKIPPED"
    url = f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/run/@cf/meta/llama-3.1-8b-instruct"
    headers = {"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}", "Content-Type": "application/json"}
    payload = {"messages": [{"role": "user", "content": TEST_PROMPT}], "max_tokens": 5, "temperature": 0.0}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        if r.status_code == 200:
            return r.json().get("result", {}).get("response", "").strip()
        return f"ERROR {r.status_code}"
    except Exception as e:
        return f"EXCEPTION {e}"

def test_gemini():
    if not GEMINI_API_KEY:
        return "SKIPPED"
    model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash-lite")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    payload = {"contents": [{"parts": [{"text": TEST_PROMPT}]}], "generationConfig": {"temperature": 0.0, "maxOutputTokens": 5}}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        if r.status_code == 200:
            return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        return f"ERROR {r.status_code}"
    except Exception as e:
        return f"EXCEPTION {e}"

def test_openrouter():
    if not OPENROUTER_API_KEY:
        return "SKIPPED"
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": "mistralai/mistral-7b-instruct:free", "messages": [{"role": "user", "content": TEST_PROMPT}], "max_tokens": 5, "temperature": 0.0}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"].strip()
        return f"ERROR {r.status_code}"
    except Exception as e:
        return f"EXCEPTION {e}"

def test_groq():
    if not GROQ_API_KEY:
        return "SKIPPED"
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": "llama-3.1-8b-instant", "messages": [{"role": "user", "content": TEST_PROMPT}], "max_tokens": 5, "temperature": 0.0}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"].strip()
        return f"ERROR {r.status_code}"
    except Exception as e:
        return f"EXCEPTION {e}"

def test_huggingface():
    if not HF_API_KEY:
        return "SKIPPED"
    url = "https://api-inference.huggingface.co/models/google/gemma-2-9b-it"
    headers = {"Authorization": f"Bearer {HF_API_KEY}"}
    payload = {"inputs": TEST_PROMPT, "parameters": {"max_new_tokens": 5, "temperature": 0.0, "return_full_text": False}}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10, verify=False)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                return data[0].get("generated_text", "").strip()
            return data.get("generated_text", "").strip()
        return f"ERROR {r.status_code}"
    except Exception as e:
        return f"EXCEPTION {e}"

def main():
    tests = [
        ("Cloudflare", test_cloudflare),
        ("Gemini", test_gemini),
        ("OpenRouter", test_openrouter),
        ("Groq", test_groq),
        ("HuggingFace", test_huggingface),
    ]
    print("\n=== PROVIDER STATUS ===\n")
    for name, func in tests:
        print(f"{name:12} ", end="")
        result = func()
        status = "✅ OK" if "OK" in result else ("⚠️ SKIPPED" if "SKIPPED" in result else "❌ FAIL")
        print(f"{status} - {result[:60]}")
    print("\nNote: 'OK' means the provider returned the word OK.")
if __name__ == "__main__":
    main()