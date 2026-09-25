"""
Unit tests for core/webhook_pipeline.py.
=========================================
Covers signing, inbound auth, SSRF protection, download, callback, and
the router factory. Uses the sync `client` fixture from conftest — no
real network, no real DB.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════════════════
# Section: _sign
# ═══════════════════════════════════════════════════════════════════════════

def test_sign_is_deterministic():
    from core.webhook_pipeline import _sign
    s1 = _sign("secret", 1700000000, b'{"a":1}')
    s2 = _sign("secret", 1700000000, b'{"a":1}')
    assert s1 == s2
    assert isinstance(s1, str)
    assert len(s1) == 64  # hex sha256


def test_sign_matches_manual_hmac():
    from core.webhook_pipeline import _sign
    secret = "s"
    ts = 1700000000
    body = b'{"x":1}'
    expected = hmac.new(
        secret.encode(), f"{ts}.".encode() + body, hashlib.sha256
    ).hexdigest()
    assert _sign(secret, ts, body) == expected


def test_sign_changes_with_body():
    from core.webhook_pipeline import _sign
    a = _sign("s", 1, b"a")
    b = _sign("s", 1, b"b")
    assert a != b


# ═══════════════════════════════════════════════════════════════════════════
# Section: _verify_inbound
# ═══════════════════════════════════════════════════════════════════════════

def test_verify_inbound_dev_mode_allows_without_creds(monkeypatch):
    monkeypatch.setenv("ENV", "dev")
    from core.webhook_pipeline import _verify_inbound
    # Should not raise when nothing is configured in dev
    _verify_inbound(
        b"body",
        authorization=None,
        signature_header=None,
        timestamp_header=None,
        shared_token="",
        shared_secret="",
    )


def test_verify_inbound_prod_refuses_without_creds(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    from fastapi import HTTPException
    from core.webhook_pipeline import _verify_inbound
    with pytest.raises(HTTPException) as exc:
        _verify_inbound(
            b"body",
            authorization=None,
            signature_header=None,
            timestamp_header=None,
            shared_token="",
            shared_secret="",
        )
    assert exc.value.status_code == 503


def test_verify_inbound_bearer_success():
    from core.webhook_pipeline import _verify_inbound
    _verify_inbound(
        b"body",
        authorization="Bearer test-token",
        signature_header=None,
        timestamp_header=None,
        shared_token="test-token",
        shared_secret="",
    )


def test_verify_inbound_bearer_wrong_token():
    from fastapi import HTTPException
    from core.webhook_pipeline import _verify_inbound
    with pytest.raises(HTTPException) as exc:
        _verify_inbound(
            b"body",
            authorization="Bearer wrong",
            signature_header=None,
            timestamp_header=None,
            shared_token="test-token",
            shared_secret="",
        )
    assert exc.value.status_code == 401


def test_verify_inbound_hmac_success():
    from core.webhook_pipeline import _sign, _verify_inbound
    secret = "s3cr3t"
    ts = int(time.time())
    body = b'{"hello":"world"}'
    sig = _sign(secret, ts, body)
    _verify_inbound(
        body,
        authorization=None,
        signature_header=f"sha256={sig}",
        timestamp_header=str(ts),
        shared_token="",
        shared_secret=secret,
    )


def test_verify_inbound_hmac_wrong_signature():
    from fastapi import HTTPException
    from core.webhook_pipeline import _verify_inbound
    with pytest.raises(HTTPException) as exc:
        _verify_inbound(
            b"body",
            authorization=None,
            signature_header="sha256=deadbeef",
            timestamp_header=str(int(time.time())),
            shared_token="",
            shared_secret="s3cr3t",
        )
    assert exc.value.status_code == 401


def test_verify_inbound_hmac_timestamp_skew_rejected():
    from fastapi import HTTPException
    from core.webhook_pipeline import _sign, _verify_inbound
    secret = "s"
    old_ts = int(time.time()) - 10_000
    body = b"body"
    sig = _sign(secret, old_ts, body)
    with pytest.raises(HTTPException) as exc:
        _verify_inbound(
            body,
            authorization=None,
            signature_header=f"sha256={sig}",
            timestamp_header=str(old_ts),
            shared_token="",
            shared_secret=secret,
        )
    assert exc.value.status_code == 401


def test_verify_inbound_invalid_timestamp_header():
    from fastapi import HTTPException
    from core.webhook_pipeline import _verify_inbound
    with pytest.raises(HTTPException):
        _verify_inbound(
            b"body",
            authorization=None,
            signature_header="sha256=x",
            timestamp_header="not-an-int",
            shared_token="",
            shared_secret="s",
        )


# ═══════════════════════════════════════════════════════════════════════════
# Section: _assert_public_url (SSRF)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_assert_public_url_rejects_non_http_scheme():
    from fastapi import HTTPException
    from core.webhook_pipeline import _assert_public_url
    with pytest.raises(HTTPException) as exc:
        await _assert_public_url("ftp://example.com/x")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_assert_public_url_rejects_loopback():
    from fastapi import HTTPException
    from core.webhook_pipeline import _assert_public_url
    with pytest.raises(HTTPException) as exc:
        await _assert_public_url("http://127.0.0.1/x")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_assert_public_url_rejects_private():
    from fastapi import HTTPException
    from core.webhook_pipeline import _assert_public_url
    with pytest.raises(HTTPException):
        await _assert_public_url("http://10.0.0.1/x")


@pytest.mark.asyncio
async def test_assert_public_url_rejects_missing_host():
    from fastapi import HTTPException
    from core.webhook_pipeline import _assert_public_url
    with pytest.raises(HTTPException) as exc:
        await _assert_public_url("http:///x")
    assert exc.value.status_code == 400


# ═══════════════════════════════════════════════════════════════════════════
# Section: _download_file (mocked HTTP)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_download_file_extracts_filename_and_mimetype():
    from core.webhook_pipeline import _download_file

    # Mock httpx AsyncClient.stream() context manager
    mock_resp = MagicMock()
    mock_resp.raise_for_status = lambda: None
    mock_resp.headers = {
        "content-disposition": 'attachment; filename="report.pdf"',
        "content-type": "application/pdf; charset=binary",
    }

    async def _aiter(_n):
        yield b"PDF-HEADER"
        yield b"-AND-BODY"
    mock_resp.aiter_bytes = _aiter

    class _StreamCtx:
        async def __aenter__(self): return mock_resp
        async def __aexit__(self, *a): return False

    mock_client = MagicMock()
    mock_client.stream = lambda *a, **kw: _StreamCtx()

    result = await _download_file(mock_client, "https://example.com/x")
    assert result["filename"] == "report.pdf"
    assert result["mimetype"] == "application/pdf"
    assert result["size"] == len(b"PDF-HEADER-AND-BODY")


@pytest.mark.asyncio
async def test_download_file_rejects_oversized_body():
    from core.webhook_pipeline import _download_file

    mock_resp = MagicMock()
    mock_resp.raise_for_status = lambda: None
    mock_resp.headers = {}
    async def _aiter(_n):
        for _ in range(10):
            yield b"x" * 1024
    mock_resp.aiter_bytes = _aiter

    class _StreamCtx:
        async def __aenter__(self): return mock_resp
        async def __aexit__(self, *a): return False

    mock_client = MagicMock()
    mock_client.stream = lambda *a, **kw: _StreamCtx()

    with pytest.raises(RuntimeError, match="exceeds"):
        await _download_file(mock_client, "https://x", max_bytes=1024)


@pytest.mark.asyncio
async def test_download_file_rejects_empty_body():
    from core.webhook_pipeline import _download_file

    mock_resp = MagicMock()
    mock_resp.raise_for_status = lambda: None
    mock_resp.headers = {}
    async def _aiter(_n):
        if False: yield b""  # noqa
    mock_resp.aiter_bytes = _aiter

    class _StreamCtx:
        async def __aenter__(self): return mock_resp
        async def __aexit__(self, *a): return False

    mock_client = MagicMock()
    mock_client.stream = lambda *a, **kw: _StreamCtx()

    with pytest.raises(RuntimeError, match="empty"):
        await _download_file(mock_client, "https://x")


# ═══════════════════════════════════════════════════════════════════════════
# Section: _post_callback
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_post_callback_signs_payload(monkeypatch):
    from core.webhook_pipeline import _post_callback

    monkeypatch.setenv("ENV", "production")

    captured = {}
    mock_resp = MagicMock()
    mock_resp.status_code = 200

    async def _post(url, headers=None, content=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = content
        return mock_resp

    mock_client = MagicMock()
    mock_client.post = _post

    ok = await _post_callback(
        mock_client, "https://hook.example/x",
        {"job": "1"}, secret="shh", pipeline_id="p1",
    )
    assert ok is True
    assert "X-Axelr-Signature" in captured["headers"]
    assert captured["headers"]["X-Axelr-Signature"].startswith("sha256=")


@pytest.mark.asyncio
async def test_post_callback_prod_refuses_without_secret(monkeypatch):
    from core.webhook_pipeline import _post_callback
    monkeypatch.setenv("ENV", "production")

    mock_client = MagicMock()
    ok = await _post_callback(
        mock_client, "https://hook.example/x",
        {}, secret="", pipeline_id="p1",
    )
    assert ok is False


@pytest.mark.asyncio
async def test_post_callback_retries_on_5xx(monkeypatch):
    from core.webhook_pipeline import _post_callback
    monkeypatch.setenv("ENV", "production")

    calls = {"n": 0}
    mock_resp_fail = MagicMock()
    mock_resp_fail.status_code = 500
    mock_resp_ok = MagicMock()
    mock_resp_ok.status_code = 200

    async def _post(*a, **kw):
        calls["n"] += 1
        return mock_resp_fail if calls["n"] < 3 else mock_resp_ok

    mock_client = MagicMock()
    mock_client.post = _post

    with patch("core.webhook_pipeline.asyncio.sleep", new=AsyncMock()):
        ok = await _post_callback(
            mock_client, "https://hook.example/x",
            {"j": 1}, secret="s", pipeline_id="p1",
        )
    assert ok is True
    assert calls["n"] == 3


# ═══════════════════════════════════════════════════════════════════════════
# Section: extract webhook payload (Pydantic schema)
# ═══════════════════════════════════════════════════════════════════════════

def test_extract_payload_valid():
    from core.webhook_pipeline import ExtractWebhookPayload
    p = ExtractWebhookPayload(
        file_url="https://example.com/a.pdf",
        pipeline_id="inv-v3",
        callback_url="https://example.com/hook",
    )
    assert p.workspace == "data"
    assert p.task_type == "extraction"
    assert p.max_tokens == 4096
    assert p.temperature == 0.2


def test_extract_payload_rejects_short_file_url():
    from pydantic import ValidationError
    from core.webhook_pipeline import ExtractWebhookPayload
    with pytest.raises(ValidationError):
        ExtractWebhookPayload(
            file_url="http://x",
            pipeline_id="p",
            callback_url="https://example.com/hook",
        )


def test_extract_payload_rejects_bad_max_tokens():
    from pydantic import ValidationError
    from core.webhook_pipeline import ExtractWebhookPayload
    with pytest.raises(ValidationError):
        ExtractWebhookPayload(
            file_url="https://example.com/a.pdf",
            pipeline_id="p",
            callback_url="https://example.com/hook",
            max_tokens=10,  # below ge=64
        )


# ═══════════════════════════════════════════════════════════════════════════
# Section: make_webhook_router (router factory)
# ═══════════════════════════════════════════════════════════════════════════

def test_make_webhook_router_returns_router():
    from fastapi import APIRouter
    from core.webhook_pipeline import make_webhook_router

    async def _stub_route(**kwargs):
        return {"success": True, "text": "ok"}

    router = make_webhook_router(_stub_route)
    assert isinstance(router, APIRouter)
    paths = [r.path for r in router.routes]
    assert "/webhook/extract" in paths


def test_make_webhook_router_endpoint_requires_auth_in_prod(monkeypatch):
    """
    In production without auth creds, POST /webhook/extract must 503.
    We rebuild the app with production ENV to exercise this path.
    """
    monkeypatch.setenv("ENV", "production")
    monkeypatch.delenv("WEBHOOK_INBOUND_TOKEN", raising=False)
    monkeypatch.delenv("WEBHOOK_INBOUND_SECRET", raising=False)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.webhook_pipeline import make_webhook_router

    async def _stub_route(**kwargs):
        return {"success": True, "text": "x"}

    app = FastAPI()
    app.include_router(make_webhook_router(_stub_route))
    client = TestClient(app)

    r = client.post("/webhook/extract", json={
        "file_url": "https://example.com/a.pdf",
        "pipeline_id": "p",
        "callback_url": "https://example.com/hook",
    })
    # 503 (auth not configured) or 401 (blocked) — must NOT be 202
    assert r.status_code in (401, 403, 503)


def test_make_webhook_router_rejects_bad_payload():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.webhook_pipeline import make_webhook_router

    async def _stub_route(**kwargs):
        return {"success": True, "text": "x"}

    app = FastAPI()
    app.include_router(make_webhook_router(_stub_route))
    client = TestClient(app)

    r = client.post("/webhook/extract", json={})
    # 422 validation OR 401 (if auth fails before validation)
    assert r.status_code in (401, 403, 422)


# ═══════════════════════════════════════════════════════════════════════════
# Section: _is_prod
# ═══════════════════════════════════════════════════════════════════════════

def test_is_prod_env_variants(monkeypatch):
    from core.webhook_pipeline import _is_prod
    for env in ("dev", "local", "test", "development", "DEV", "Test"):
        monkeypatch.setenv("ENV", env)
        assert _is_prod() is False, f"{env} should be non-prod"
    for env in ("production", "prod", "staging", "PROD"):
        monkeypatch.setenv("ENV", env)
        assert _is_prod() is True, f"{env} should be prod"


def test_is_prod_defaults_to_dev(monkeypatch):
    monkeypatch.delenv("ENV", raising=False)
    from core.webhook_pipeline import _is_prod
    assert _is_prod() is False