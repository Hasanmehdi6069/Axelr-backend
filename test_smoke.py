import pytest


@pytest.mark.asyncio
async def test_root_returns_200(client):
    r = await client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert "status" in body
    assert "uptime" in body

@pytest.mark.asyncio
async def test_diagnose_requires_admin(client):
    r = await client.get("/api/v1/diagnose")
    # 401 without a token, 403 with a non-admin token — either is correct
    assert r.status_code in (401, 403)

@pytest.mark.asyncio
async def test_validation_error_shape(client):
    r = await client.post("/api/auth/email", json={"email": "x"})
    assert r.status_code == 422
    assert r.json()["code"] == "VALIDATION_ERROR"

@pytest.mark.asyncio
async def test_touch_fix_engine_loads():
    from core.touch_fix import TouchFixEngine
    e = TouchFixEngine()
    assert e.apply_diff("a\nb\nc\n", "--- original\n+++ fixed\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n") == "a\nB\nc\n"

@pytest.mark.asyncio
async def test_touch_fix_stub_is_gone():
    from core.touch_fix import TouchFixEngine
    assert hasattr(TouchFixEngine, "fix_block")
    assert hasattr(TouchFixEngine, "apply_diff")