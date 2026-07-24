# main.py
# Single Entrypoint for Axelr Python Backend Orchestrator

import importlib
import sys
from axelr_scheduler import app

if __name__ == "__main__":
    try:
        uvicorn = importlib.import_module("uvicorn")
    except ModuleNotFoundError:
        print("Error: uvicorn is not installed. Install it with `pip install uvicorn`.")
        sys.exit(1)
    print("🟢 Starting Axelr AI Orchestrator on http://0.0.0.0:5001")
    uvicorn.run("axelr_scheduler:app", host="0.0.0.0", port=5001, reload=True)