#!/usr/bin/env python3
"""
Continuously monitor provider health by polling /api/v1/diagnose.
"""

import sys
import time

import requests

BASE_URL = "https://axelr-backend.onrender.com"
INTERVAL = 60  # seconds

def get_status():
    url = f"{BASE_URL}/api/v1/diagnose"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("providers", {})
        return None
    except Exception as e:
        print(f"Error: {e}")
        return None

def main():
    print(f"Monitoring providers every {INTERVAL}s. Press Ctrl+C to stop.")
    while True:
        providers = get_status()
        if providers:
            print("\n" + time.strftime("%Y-%m-%d %H:%M:%S"))
            for name, status in providers.items():
                s = status.get("status", "unknown")
                if s == "healthy":
                    emoji = "✅"
                elif s == "skipped":
                    emoji = "⏭️"
                elif s == "error":
                    emoji = "❌"
                else:
                    emoji = "⚠️"
                print(f"{emoji} {name:>15} : {s}")
        else:
            print("⚠️ Could not fetch provider status.")
        try:
            time.sleep(INTERVAL)
        except KeyboardInterrupt:
            print("\nMonitoring stopped.")
            sys.exit(0)

if __name__ == "__main__":
    main()