"""
AXELR — Entry shim. Canonical app lives in ``api.warhead``.

    uvicorn app:app                 # render.yaml (kept for compat)
    uvicorn api.warhead:app         # canonical
"""
from api.warhead import app, create_app  # noqa: F401

__all__ = ["app", "create_app"]

if __name__ == "__main__":
    import os
    import uvicorn
    uvicorn.run(app, host="0.0.0.0",
                port=int(os.getenv("PORT", 8000)), log_level="info")