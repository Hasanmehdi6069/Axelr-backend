#!/usr/bin/env python3
"""
Quick diagnostic – calls the /api/v1/diagnose endpoint.
"""


import requests

BASE_URL = "https://axelr-backend.onrender.com"  # or localhost:8000

def main():
    url = f"{BASE_URL}/api/v1/diagnose"
    try:
        resp = requests.get(url, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            print("Provider Status:\n" + "-"*40)
            for provider, status in data.get("providers", {}).items():
                s = status.get("status", "unknown")
                if s == "healthy":
                    status_str = f"✅ {s} ({status.get('latency_ms', 'N/A')} ms)"
                elif s == "skipped":
                    status_str = f"⏭️ {s} ({status.get('reason', '')})"
                elif s == "error":
                    status_str = f"❌ {s} ({status.get('error', '')})"
                else:
                    status_str = f"⚠️ {s}"
                print(f"{provider:>15} : {status_str}")
        else:
            print(f"Error: {resp.status_code} – {resp.text}")
    except Exception as e:
        print(f"Request failed: {e}")

if __name__ == "__main__":
    main()