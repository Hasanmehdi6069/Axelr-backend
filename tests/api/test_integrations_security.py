"""
Integrations & security contract tests.

Covers:
  • /api/tools/* — AI-error-shape guard, timeout, validation, auth
  • /api/execute-code — size + timeout clamping
  • /api/touch_fix — diff path, error message requirement
  • /api/summarize-chat, /api/refine-response — DB-absence 5xx guard
  • /api/suggestions — unknown workspace fallback
  • /api/qstash/* — HMAC verify, DLQ auth, payload-size-before-buffer, SSRF
  • Provider HTTP adapters — NameError regression (payload defined in `if`)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json

import pytest

# ═══════════════════════════════════════════════════════════════════════════
# PART A — TOOLS
# ═══════════════════════════════════════════════════════════════════════════

# ── A1. hard_timeout decorator ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hard_timeout_raises_504_on_slow_handler():
    from fastapi import HTTPException

    from api.routes_tools import hard_timeout

    @hard_timeout(seconds=0.05)
    async def slow():
        await asyncio.sleep(1.0)

    with pytest.raises(HTTPException) as ei:
        await slow()
    assert ei.value.status_code == 504


@pytest.mark.asyncio
async def test_hard_timeout_passes_through_fast_handler():
    from api.routes_tools import hard_timeout

    @hard_timeout(seconds=1.0)
    async def fast():
        return {"ok": True}

    assert await fast() == {"ok": True}


@pytest.mark.asyncio
async def test_hard_timeout_preserves_signature():
    from api.routes_tools import hard_timeout

    @hard_timeout(seconds=1.0)
    async def with_args(a, b, c=None):
        return a + b + (c or 0)

    assert await with_args(1, 2, 3) == 6


# ── A2. Tool endpoints — parametrized over all of them ─────────────────────

TOOL_ENDPOINTS = [
    ("/api/tools/translate-code", {"code": "print(1)", "source_lang": "python", "target_lang": "javascript"}),
    ("/api/tools/mermaid", {"process_description": "A → B → C"}),
    ("/api/tools/scan-pii", {"document_text": "hello world"}),
    ("/api/tools/meeting-minutes", {"transcript": "Alice: hi\nBob: hello"}),
    ("/api/tools/decision-matrix", {"options": ["A", "B"], "criteria": ["cost", "speed"], "context": ""}),
]

_IDS = [p[0] for p in TOOL_ENDPOINTS]


@pytest.mark.parametrize("path,body", TOOL_ENDPOINTS, ids=_IDS)
def test_tool_requires_auth(client, path, body):
    r = client.post(path, json=body)
    assert r.status_code in (401, 403), f"{path} → {r.status_code} without auth"


@pytest.mark.parametrize("path,body", TOOL_ENDPOINTS, ids=_IDS)
def test_tool_handles_ai_error_shape(client, path, body, authed_headers, monkeypatch):
    """Regression: every tool does `res["text"]` — KeyError → 500 on error shape."""
    import api.routes_tools as tools_mod

    async def broken_ai(*args, **kwargs):
        return {"success": False, "error": "provider_down"}

    monkeypatch.setattr(tools_mod, "route_ai_request_parallel", broken_ai)
    r = client.post(path, json=body, headers=authed_headers)
    assert r.status_code < 500, f"{path} 500'd on error-shaped AI response (KeyError not guarded)"


@pytest.mark.parametrize("path,body", TOOL_ENDPOINTS, ids=_IDS)
def test_tool_happy_path(client, path, body, authed_headers, monkeypatch):
    import api.routes_tools as tools_mod

    async def good_ai(*args, **kwargs):
        return {"success": True, "text": "ok response"}

    monkeypatch.setattr(tools_mod, "route_ai_request_parallel", good_ai)
    r = client.post(path, json=body, headers=authed_headers)
    assert r.status_code == 200
    assert r.json()["success"] is True


# ── A3. execute-code validation ────────────────────────────────────────────


def test_execute_code_rejects_oversized(client, authed_headers):
    r = client.post("/api/execute-code", json={"language": "python", "code": "x" * 40_001}, headers=authed_headers)
    assert r.status_code == 413


@pytest.mark.parametrize("timeout", [-1, 0, 999, "not-an-int", None])
def test_execute_code_timeout_clamped_or_rejected(client, authed_headers, timeout):
    r = client.post(
        "/api/execute-code", json={"language": "python", "code": "print(1)", "timeout": timeout}, headers=authed_headers
    )
    assert r.status_code < 500, f"timeout={timeout!r} → {r.status_code}"


# ── A4. touch_fix ──────────────────────────────────────────────────────────


def test_touch_fix_diff_path(client, authed_headers):
    r = client.post(
        "/api/touch_fix",
        json={"code": "print(1)", "error_message": "", "diff": "--- a\n+++ b\n"},
        headers=authed_headers,
    )
    assert r.status_code < 500


def test_touch_fix_requires_error_message(client, authed_headers):
    r = client.post("/api/touch_fix", json={"code": "print(1)", "error_message": ""}, headers=authed_headers)
    assert r.status_code < 500


# ── A5. DB-absence guards ─────────────────────────────────────────────────


def test_summarize_chat_503_without_db(client, authed_headers, monkeypatch):
    from api.state import state

    monkeypatch.setattr(state, "db_available", False)
    r = client.post("/api/summarize-chat?session_id=507f1f77bcf86cd799439011", headers=authed_headers)
    assert r.status_code in (400, 404, 503)


def test_refine_response_503_without_db(client, authed_headers, monkeypatch):
    from api.state import state

    monkeypatch.setattr(state, "db_available", False)
    r = client.post(
        "/api/refine-response",
        json={"sessionId": "507f1f77bcf86cd799439011", "msgId": "x", "newText": "hi", "originalText": "hi"},
        headers=authed_headers,
    )
    assert r.status_code in (400, 404, 503)


def test_refine_response_rejects_invalid_objectid(client, authed_headers, monkeypatch):
    from api.state import state

    monkeypatch.setattr(state, "db_available", True)
    r = client.post(
        "/api/refine-response",
        json={"sessionId": "not-an-objectid", "msgId": "x", "newText": "a", "originalText": "a"},
        headers=authed_headers,
    )
    assert r.status_code == 400


# ── A6. suggestions ────────────────────────────────────────────────────────


@pytest.mark.parametrize("ws", ["data", "design", "core", "garbage", ""])
def test_suggestions_never_500s(client, authed_headers, ws):
    r = client.get(f"/api/suggestions?workspace={ws}", headers=authed_headers)
    assert r.status_code == 200
    body = r.json()
    assert "suggestions" in body
    assert isinstance(body["suggestions"], list)
    assert len(body["suggestions"]) > 0


def test_suggestions_unknown_workspace_falls_back_to_core(client, authed_headers):
    r1 = client.get("/api/suggestions?workspace=core", headers=authed_headers)
    r2 = client.get("/api/suggestions?workspace=garbage", headers=authed_headers)
    assert r1.json()["suggestions"] == r2.json()["suggestions"]


# ═══════════════════════════════════════════════════════════════════════════
# PART B — QSTASH WEBHOOK SECURITY
# ═══════════════════════════════════════════════════════════════════════════


def _sign(body: bytes, key: str) -> str:
    return "sha256=" + base64.b64encode(hmac.new(key.encode(), body, hashlib.sha256).digest()).decode()


class _FakeRequest:
    def __init__(self, body: bytes, headers: dict, client_ip: str = "127.0.0.1"):
        self._body = body
        self.headers = headers
        self.client = type("C", (), {"host": client_ip})()

    async def body(self):
        return self._body


@pytest.fixture
def signing_key(monkeypatch):
    monkeypatch.setenv("UPSTASH_QSTASH_CURRENT_SIGNING_KEY", "test-key-xyz")
    monkeypatch.setenv("ENVIRONMENT", "production")
    return "test-key-xyz"


# ── B1. HMAC signature verification ────────────────────────────────────────


@pytest.mark.asyncio
async def test_signature_accepts_valid(signing_key):
    from api.qstash_webhook import verify_qstash_signature

    body = b'{"task_id":"t1"}'
    req = _FakeRequest(body, {"upstash-signature": _sign(body, signing_key)})
    assert await verify_qstash_signature(req) is True


@pytest.mark.asyncio
async def test_signature_rejects_tampered(signing_key):
    from api.qstash_webhook import verify_qstash_signature

    req = _FakeRequest(b'{"task_id":"t1"}', {"upstash-signature": _sign(b"other", signing_key)})
    assert await verify_qstash_signature(req) is False


@pytest.mark.asyncio
async def test_signature_rejects_missing_header(signing_key):
    from api.qstash_webhook import verify_qstash_signature

    req = _FakeRequest(b"{}", {})
    assert await verify_qstash_signature(req) is False


@pytest.mark.asyncio
async def test_signature_rejects_empty_header(signing_key):
    from api.qstash_webhook import verify_qstash_signature

    req = _FakeRequest(b"{}", {"upstash-signature": ""})
    assert await verify_qstash_signature(req) is False


@pytest.mark.asyncio
async def test_signature_fails_closed_in_prod_when_key_missing(monkeypatch):
    monkeypatch.delenv("UPSTASH_QSTASH_CURRENT_SIGNING_KEY", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")
    from api.qstash_webhook import verify_qstash_signature

    req = _FakeRequest(b"{}", {"upstash-signature": "whatever"})
    assert await verify_qstash_signature(req) is False


@pytest.mark.asyncio
async def test_signature_accepts_in_dev_when_key_missing(monkeypatch):
    monkeypatch.delenv("UPSTASH_QSTASH_CURRENT_SIGNING_KEY", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "development")
    from api.qstash_webhook import verify_qstash_signature

    req = _FakeRequest(b"{}", {})
    assert await verify_qstash_signature(req) is True


# ── B2. DLQ auth ───────────────────────────────────────────────────────────


def test_dlq_requires_signature(client):
    r = client.post("/api/qstash/dlq", content=b'{"malicious":"payload"}', headers={"Content-Type": "application/json"})
    assert r.status_code != 200, f"DLQ accepted unsigned request: {r.status_code}"


# ── B3. Payload-size guard (must fire before body buffered) ────────────────


def test_payload_size_guard_fires_before_body_buffered(client, signing_key):
    body = b'{"task_id":"x","blob":"' + b"a" * (10 * 1024 * 1024) + b'"}'
    sig = _sign(body, signing_key)
    r = client.post(
        "/api/qstash/webhook", content=body, headers={"upstash-signature": sig, "Content-Type": "application/json"}
    )
    assert r.status_code == 413


# ── B4. SSRF guard on payload_stream_url ───────────────────────────────────


def test_ssrf_guard_exists():
    from api import qstash_webhook

    assert hasattr(qstash_webhook, "_is_url_allowed"), "no _is_url_allowed() guard on payload_stream_url"


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8080/internal",
        "http://localhost/admin",
        "http://10.0.0.1/admin",
        "http://192.168.1.1/router",
        "http://172.16.0.1/x",
        "file:///etc/passwd",
        "gopher://0.0.0.0",
        "ftp://internal",
    ],
)
def test_ssrf_guard_blocks_dangerous_urls(url):
    from api import qstash_webhook

    validator = getattr(qstash_webhook, "_is_url_allowed", None)
    if validator is None:
        pytest.fail("_is_url_allowed not implemented — SSRF guard missing")
    assert validator(url) is False, f"guard let through {url}"


@pytest.mark.parametrize(
    "url",
    [
        "https://storage.googleapis.com/bucket/file.bin",
        "https://example.com/payload",
    ],
)
def test_ssrf_guard_allows_https(url):
    from api import qstash_webhook

    validator = getattr(qstash_webhook, "_is_url_allowed", None)
    if validator is None:
        pytest.fail("_is_url_allowed not implemented")
    assert validator(url) is True, f"guard rejected safe HTTPS: {url}"


# ── B5. Payload validation ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"task_id": "t"},
        {"task_id": "t", "task_type": "invalid"},
        {"task_id": "t", "task_type": "embedding", "token_chunk": "not-a-list"},
    ],
)
def test_invalid_payload_rejected(client, signing_key, payload):
    body = json.dumps(payload).encode()
    sig = _sign(body, signing_key)
    r = client.post(
        "/api/qstash/webhook", content=body, headers={"upstash-signature": sig, "Content-Type": "application/json"}
    )
    assert r.status_code in (400, 422), f"invalid payload accepted: {payload} → {r.status_code}"


def test_valid_health_task(client, signing_key):
    body = json.dumps({"task_id": "t", "task_type": "health"}).encode()
    sig = _sign(body, signing_key)
    r = client.post(
        "/api/qstash/webhook", content=body, headers={"upstash-signature": sig, "Content-Type": "application/json"}
    )
    assert r.status_code == 200
    assert r.json()["status"] == "completed"


def test_webhook_rejects_invalid_signature(client, signing_key):
    body = b'{"task_id":"t","task_type":"health"}'
    r = client.post(
        "/api/qstash/webhook",
        content=body,
        headers={"upstash-signature": "sha256=AAAA", "Content-Type": "application/json"},
    )
    assert r.status_code == 403


# ── B6. Task payload model ─────────────────────────────────────────────────


def test_payload_model_is_write_operation():
    from api.qstash_webhook import QStashTaskPayload

    storage = QStashTaskPayload(task_id="t", task_type="storage")
    persistence = QStashTaskPayload(task_id="t", task_type="persistence")
    embedding = QStashTaskPayload(task_id="t", task_type="embedding")
    assert storage.is_write_operation is True
    assert persistence.is_write_operation is True
    assert embedding.is_write_operation is False


@pytest.mark.parametrize("bad_type", ["", "not_real", "STORAGE", "Storage!"])
def test_payload_model_rejects_bad_type(bad_type):
    from pydantic import ValidationError

    from api.qstash_webhook import QStashTaskPayload

    with pytest.raises(ValidationError):
        QStashTaskPayload(task_id="t", task_type=bad_type)


# ═══════════════════════════════════════════════════════════════════════════
# PART C — PROVIDER HTTP ADAPTERS
# ═══════════════════════════════════════════════════════════════════════════


def test_openai_compat_no_extra_headers_does_not_crash():
    """
    Regression: `payload` was defined inside `if extra_headers:` so any
    provider without extra_headers raised NameError on first call — 18+
    providers were dead. This test would have caught it.
    """
    pytest.importorskip("respx")
    import respx
    from httpx import Response

    from api.provider_http import _openai_compat

    with respx.mock:
        respx.post("https://api.example.invalid/v1/chat/completions").mock(
            return_value=Response(200, json={"choices": [{"message": {"content": "ok"}}]})
        )
        adapter = _openai_compat(
            name="t",
            base_url="https://api.example.invalid/v1/chat/completions",
            key="k",
            default_model="m",
        )
        text = asyncio.run(adapter("hi", 16, 0.0, None))
        assert text == "ok"


def test_openai_compat_missing_key_raises():
    from api.provider_http import _openai_compat

    adapter = _openai_compat(name="groq", base_url="https://x", key="")
    with pytest.raises(Exception, match="API key missing"):
        asyncio.run(adapter("hi", 16, 0.0, None))


def test_provider_http_registry_contains_required_names():
    from api.provider_http import PROVIDER_HTTP

    for name in ("gemini", "groq", "cloudflare", "openrouter", "mistral", "local"):
        assert name in PROVIDER_HTTP
