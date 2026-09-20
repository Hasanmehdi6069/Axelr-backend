import os

import httpx
import pytest

os.environ.setdefault("ENV", "dev")
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-production")
os.environ.setdefault("MONGO_URI", "")           # run without DB
os.environ.setdefault("REDIS_URL", "")

@pytest.fixture
async def client():
    from app import app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c