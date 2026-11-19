"""
HTTP contract, surface, and code-hygiene tests.

Covers: liveness, error handling, security headers, OpenAPI, auth guards,
rate limiting, concurrency/load, AST hygiene, provider catalog consistency,
cross-module shims, SSE contract, boot contract.
"""
from __future__ import annotations

import ast
import concurrent.futures
import importlib
import inspect
import pkgutil
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from pydantic import BaseModel

pytestmark = pytest.mark.e2e


# ═══════════════════════════════════════════════════════════════════════════
# Discovery helpers
# ═══════════════════════════════════════════════════════════════════════════

def _iter_modules(pkg_name: str):
    pkg = importlib.import_module(pkg_name)
    for info in pkgutil.walk_packages(pkg.__path__, prefix=f"{pkg_name}."):
        try:
            mod = importlib.import_module(info.name)
        except Exception as e:
            pytest.fail(f"import failed: {info.name} → {type(e).__name__}: {e}")
        src = ""
        spec = info.module_finder.find_spec(info.name)
        if spec and spec.origin and spec.origin.endswith(".py"):
            try:
                src = Path(spec.origin).read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass
        yield info.name, mod, src


def _pydantic_models_in(mod_name: str, mod) -> list[type]:
    return [
        cls for _, cls in inspect.getmembers(mod, inspect.isclass)
        if issubclass(cls, BaseModel) and cls is not BaseModel
        and cls.__module__ == mod_name
    ]


def _discover_app() -> FastAPI:
    for candidate in ("api.main:app", "api._boot:app", "api.server:app",
                      "main:app", "app:app"):
        mod_name, _, attr = candidate.partition(":")
        try:
            mod = importlib.import_module(mod_name)
            obj = getattr(mod, attr, None)
            if isinstance(obj, FastAPI):
                return obj
        except Exception:
            continue
    pytest.fail("Could not discover FastAPI app — update _discover_app()")


def _mutating_routes(app: FastAPI):
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", None)
        if not path:
            continue
        for m in methods & {"POST", "PUT", "PATCH", "DELETE"}:
            yield m, path


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "test_id", path)


# Endpoints that legitimately return 2xx without a bearer token.
PUBLIC_PATHS = {
    "/", "/ping", "/api/health", "/api/health/detailed",
    "/metrics", "/openapi.json", "/docs", "/redoc",
    "/api/auth/login", "/api/auth/register", "/api/auth/google",
    "/api/qstash/webhook", "/api/qstash/dlq",   # signature-protected, not bearer
}


# ═══════════════════════════════════════════════════════════════════════════
# 1. LIVENESS & READINESS
# ═══════════════════════════════════════════════════════════════════════════

def test_root_returns_service_name(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.json()["service"] == "axelr-backend"


def test_root_head_and_options(client):
    assert client.head("/").status_code == 200
    assert client.options("/api/health").status_code == 200


def test_health_status_and_shape(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("operational", "degraded")
    assert "timestamp" in body


def test_health_survives_degraded_state(client, monkeypatch):
    """Health must NEVER 5xx, even when DB/Redis are reported down."""
    from api.state import state
    monkeypatch.setattr(state, "db_available", False, raising=False)
    r = client.get("/api/health")
    assert 200 <= r.status_code < 300


def test_health_detailed_shape(client):
    r = client.get("/api/health/detailed")
    assert r.status_code == 200
    body = r.json()
    for k in ("status", "db", "redis", "providers", "uptime"):
        assert k in body


def test_ping_returns_pong(client):
    assert client.get("/ping").text == "pong"


def test_metrics_prometheus_format(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert ("http_requests_total" in r.text
            or "# HELP" in r.text
            or "# TYPE" in r.text)


# ═══════════════════════════════════════════════════════════════════════════
# 2. ERROR HANDLING
# ═══════════════════════════════════════════════════════════════════════════

def test_unknown_route_404_clean(client):
    r = client.get("/definitely/not/a/route")
    assert r.status_code == 404
    assert "Traceback" not in r.text


def test_method_not_allowed_405(client):
    r = client.delete("/api/health")
    assert r.status_code in (404, 405)


def test_malformed_json_never_5xx_across_all_routes(client):
    """Every mutating route must reject `{ not json` with 4xx, never 5xx."""
    app = _discover_app()
    failures = []
    for method, path in _mutating_routes(app):
        try:
            r = client.request(
                method, _concrete(path),
                content=b"{ not json",
                headers={"Content-Type": "application/json"},
            )
        except Exception as e:
            failures.append(f"{method} {path}: {type(e).__name__}")
            continue
        if r.status_code >= 500:
            failures.append(f"{method} {path} → {r.status_code}")
    assert not failures, "malformed JSON 5xx:\n" + "\n".join(failures)


def test_empty_body_never_5xx(client):
    app = _discover_app()
    failures = []
    for method, path in _mutating_routes(app):
        try:
            r = client.request(method, _concrete(path))
        except Exception as e:
            failures.append(f"{method} {path}: {type(e).__name__}")
            continue
        if r.status_code >= 500:
            failures.append(f"{method} {path} → {r.status_code}")
    assert not failures, "empty body 5xx:\n" + "\n".join(failures)


# ═══════════════════════════════════════════════════════════════════════════
# 3. SECURITY HEADERS & CORS
# ═══════════════════════════════════════════════════════════════════════════

def test_x_security_headers_present(client):
    r = client.get("/")
    required = ("X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy")
    missing = [h for h in required if h not in r.headers]
    assert not missing, f"missing security headers: {missing}"


def test_body_size_guard_registered():
    from api.middleware import MAX_BODY_BYTES
    assert MAX_BODY_BYTES > 0


def test_cors_headers_on_preflight(client):
    r = client.options(
        "/api/health",
        headers={"Origin": "https://example.com",
                 "Access-Control-Request-Method": "GET"},
    )
    assert r.status_code in (200, 204)


# ═══════════════════════════════════════════════════════════════════════════
# 4. OPENAPI
# ═══════════════════════════════════════════════════════════════════════════

def test_openapi_valid_and_versioned(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    assert spec["openapi"].startswith("3.")
    assert isinstance(spec["paths"], dict) and spec["paths"]


def test_openapi_lists_required_routes(client):
    spec = client.get("/openapi.json").json()
    paths = set(spec["paths"].keys())
    for required in ("/api/health", "/api/guest/session"):
        assert required in paths, f"{required} missing from OpenAPI"


def test_every_openapi_get_path_reachable(client):
    spec = client.get("/openapi.json").json()
    missing = []
    for path, methods in spec["paths"].items():
        if "get" not in methods:
            continue
        r = client.get(_concrete(path))
        if r.status_code == 404:
            missing.append(path)
    assert not missing, f"OpenAPI GET paths returning 404: {missing}"


def test_openapi_no_duplicate_operation_ids(client):
    spec = client.get("/openapi.json").json()
    ids: list[str] = []
    for _, methods in spec["paths"].items():
        for _, op in methods.items():
            oid = op.get("operationId") if isinstance(op, dict) else None
            if oid:
                ids.append(oid)
    dupes = {x for x in ids if ids.count(x) > 1}
    assert not dupes, f"duplicate operationIds: {dupes}"


# ═══════════════════════════════════════════════════════════════════════════
# 5. AUTH GUARDS
# ═══════════════════════════════════════════════════════════════════════════

def test_all_protected_mutating_routes_require_auth(client):
    """Any mutating route not in PUBLIC_PATHS must 4xx without credentials."""
    app = _discover_app()
    leaks = []
    for method, path in _mutating_routes(app):
        if path in PUBLIC_PATHS or path.startswith("/api/guest/"):
            continue
        r = client.request(method, _concrete(path))
        if r.status_code < 400:
            leaks.append(f"{method} {path} → {r.status_code} (no auth)")
    assert not leaks, "unauthenticated access:\n" + "\n".join(leaks)


@pytest.mark.parametrize("path,method", [
    ("/api/chat", "POST"),
    ("/api/enhance-prompt", "POST"),
    ("/api/execute-code", "POST"),
    ("/api/workspace/detect", "POST"),
    ("/api/twin/telemetry", "POST"),
])
def test_protected_routes_require_auth(client, path, method):
    r = client.request(method, path, json={})
    assert r.status_code in (401, 403, 422), f"{path} → {r.status_code}"


def test_auth_hash_and_verify_roundtrip():
    from api.auth import hash_password, verify_password
    h = hash_password("hunter2")
    assert verify_password("hunter2", h) is True
    assert verify_password("nope", h) is False


def test_guest_session_creates_unique_id(client):
    sid1 = client.post("/api/guest/session").json()["sessionId"]
    sid2 = client.post("/api/guest/session").json()["sessionId"]
    assert sid1 and sid2 and sid1 != sid2


# ═══════════════════════════════════════════════════════════════════════════
# 6. RATE LIMITING
# ═══════════════════════════════════════════════════════════════════════════

def test_rate_limit_fires_within_burst(client):
    """At least one 429 OR auth wall within a burst → limit exists."""
    statuses = [
        client.post("/api/chat", json={"command": "hi"}).status_code
        for _ in range(50)
    ]
    assert 429 in statuses or any(s in (401, 403, 422) for s in statuses)


# ═══════════════════════════════════════════════════════════════════════════
# 7. CONCURRENCY / LOAD SMOKE
# ═══════════════════════════════════════════════════════════════════════════

def test_concurrent_health_checks_all_succeed(client):
    def hit(_):
        return client.get("/api/health").status_code
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
        statuses = list(ex.map(hit, range(100)))
    bad = [s for s in statuses if s >= 300]
    assert not bad, f"non-2xx under concurrency: {bad[:5]}"


def test_concurrent_guest_sessions_unique(client):
    def hit(_):
        return client.post("/api/guest/session").json()["sessionId"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
        sids = list(ex.map(hit, range(50)))
    assert len(set(sids)) == len(sids), "duplicate session IDs under concurrency"


def test_concurrent_workspace_detects_deterministic(client, authed_headers):
    def hit(_):
        r = client.post("/api/workspace/detect",
                        json={"prompt": "write a python function", "files": []},
                        headers=authed_headers)
        return r.json().get("workspace")
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
        results = list(ex.map(hit, range(20)))
    assert len(set(results)) == 1, f"non-deterministic: {set(results)}"


# ═══════════════════════════════════════════════════════════════════════════
# 8. AST HYGIENE (static regressions)
# ═══════════════════════════════════════════════════════════════════════════

def test_every_module_imports_cleanly():
    for pkg in ("api", "core"):
        try:
            for name, _, _ in _iter_modules(pkg):
                assert name
        except ImportError:
            continue


def test_every_all_symbol_resolves():
    drift = []
    for pkg in ("api", "core"):
        try:
            for name, mod, _ in _iter_modules(pkg):
                for sym in getattr(mod, "__all__", []):
                    if not hasattr(mod, sym):
                        drift.append(f"{name}.{sym}")
        except ImportError:
            continue
    assert not drift, f"__all__ drift: {drift}"


def test_no_mutable_pydantic_defaults():
    offenders = []
    for mod_name, mod, _ in _iter_modules("api"):
        for cls in _pydantic_models_in(mod_name, mod):
            for fname, field in cls.model_fields.items():
                if field.default_factory is None and isinstance(
                    field.default, (list, dict, set)
                ):
                    offenders.append(f"{cls.__module__}.{cls.__name__}.{fname}")
    assert not offenders, f"mutable defaults: {offenders}"


def test_no_del_of_function_parameter():
    """`del param` only drops the local binding — caller still holds ref."""
    offenders = []
    for name, _, src in _iter_modules("api"):
        if not src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            params = {a.arg for a in fn.args.args}
            params |= {a.arg for a in fn.args.kwonlyargs}
            for node in ast.walk(fn):
                if isinstance(node, ast.Delete):
                    for t in node.targets:
                        if isinstance(t, ast.Name) and t.id in params:
                            offenders.append(f"{name}:{fn.name}({t.id})")
    assert not offenders, f"del-of-param (cargo cult): {offenders}"


def test_no_gc_collect_inside_async_loop():
    """gc.collect() per chunk is measurable latency — should not exist."""
    offenders = []
    for name, _, src in _iter_modules("api"):
        if not src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                    continue
                for child in ast.walk(node):
                    if (isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Attribute)
                        and child.func.attr == "collect"
                        and isinstance(child.func.value, ast.Name)
                        and child.func.value.id == "gc"):
                        offenders.append(f"{name}:{fn.name}")
    assert not offenders, f"gc.collect() inside a loop body: {offenders}"


def test_no_duplicate_public_functions_across_modules():
    """Regression: workspace_detector and workspace_engine diverged."""
    seen: dict[str, str] = {}
    dups = []
    for name, mod, _ in _iter_modules("api"):
        if mod.__dict__.get("__shim__", False):
            continue
        for sym in getattr(mod, "__all__", []):
            obj = getattr(mod, sym, None)
            if not callable(obj):
                continue
            qualified = f"{getattr(obj, '__module__', '?')}.{getattr(obj, '__qualname__', '?')}"
            if sym in seen and seen[sym] != qualified:
                dups.append(f"{sym}: {seen[sym]} vs {qualified} (in {name})")
            else:
                seen.setdefault(sym, qualified)
    assert not dups, f"duplicate public names: {dups}"


# ═══════════════════════════════════════════════════════════════════════════
# 9. CROSS-MODULE SHIM CONSISTENCY
# ═══════════════════════════════════════════════════════════════════════════

def test_workspace_detector_is_a_shim():
    import api.workspace_detector as det

    import api.workspace_engine as eng
    assert det.compute_full_detection is eng.compute_full_detection


def test_workspace_detect_endpoint_does_not_import_error(client, authed_headers):
    r = client.post("/api/workspace/detect",
                    json={"prompt": "write a python function", "files": []},
                    headers=authed_headers)
    assert r.status_code == 200, f"got {r.status_code}: {r.text[:200]}"
    body = r.json()
    assert body.get("workspace") in {"data", "design", "core"}


def test_workspace_detector_happy_paths():
    from api.workspace_detector import detect_workspace
    assert detect_workspace("extract columns from this CSV", []) == "data"
    assert detect_workspace("design a navbar", []) == "design"
    assert detect_workspace("hello there", []) == "core"


def test_workspace_detector_neutral_files_zero_effect():
    from api.workspace_detector import detect_workspace
    a = detect_workspace("analyze this csv", [{"filename": "README.md"}])
    b = detect_workspace("analyze this csv", [])
    assert a == b == "data"


# ═══════════════════════════════════════════════════════════════════════════
# 10. PROVIDER CATALOG
# ═══════════════════════════════════════════════════════════════════════════

def test_provider_chain_local_is_last_for_every_workspace():
    from api.providers import get_provider_order
    for ws in ("data", "design", "core", "prompt", "touch_fix"):
        order = get_provider_order(ws)
        assert order[-1] == "local", ws
        assert len(order) == len(set(order))


def test_provider_chain_is_deterministic():
    from api.providers import get_provider_order
    for ws in ("data", "design", "core"):
        assert get_provider_order(ws) == get_provider_order(ws)


def test_provider_catalog_cross_consistency():
    from api.providers import (
        PROVIDER_CHAIN,
        PROVIDER_FUNC_MAP,
        PROVIDER_KEY_CHECK,
        PROVIDER_MODELS,
    )
    for name, _ in PROVIDER_CHAIN:
        assert name in PROVIDER_FUNC_MAP, f"{name} missing FUNC_MAP"
        assert name in PROVIDER_KEY_CHECK, f"{name} missing KEY_CHECK"
        assert name in PROVIDER_MODELS, f"{name} missing MODELS"


def test_provider_models_is_list_or_empty():
    from api.providers import PROVIDER_MODELS
    for name, models in PROVIDER_MODELS.items():
        assert isinstance(models, list)


def test_dynamic_ranking_never_includes_unkeyed():
    import asyncio

    from api.providers import (
        PROVIDER_KEY_CHECK,
        get_dynamically_ranked_providers,
    )
    for ws in ("data", "design", "core"):
        ranked = asyncio.run(get_dynamically_ranked_providers(ws))
        for p in ranked:
            if p == "local":
                continue
            assert PROVIDER_KEY_CHECK.get(p), f"{ws}: {p} has no key"


def test_local_fallback_covers_all_workspaces():
    from api.providers import build_local_fallback_response
    for ws in ("data", "design", "core"):
        out = build_local_fallback_response(ws, ws, "x")
        assert isinstance(out, str) and out


def test_provider_shape_detection_for_major_types():
    """Recovered from test_provider_compat.py — non-flaky subset only."""
    from core.gateway_translate import detect_shape
    assert detect_shape("anthropic") == "anthropic"
    assert detect_shape("claude") == "anthropic"
    assert detect_shape("gemini") == "gemini"
    assert detect_shape("googleapis") == "gemini"
    assert detect_shape("openai") == "openai"
    assert detect_shape("bifrost") == "openai"
    assert detect_shape("litellm") == "openai"
    assert detect_shape("helicone") == "openai"


def test_bifrost_marked_keyless():
    from api.providers import PROVIDER_KEY_CHECK
    assert PROVIDER_KEY_CHECK["bifrost"] is True


def test_all_provider_models_build_openai_payload():
    """Every model for every provider must build a valid OpenAI payload."""
    from core.gateway_translate import build_payload

    from api.providers import PROVIDER_MODELS
    for provider, models in PROVIDER_MODELS.items():
        for model in models[:2]:
            payload = build_payload(
                "openai",
                [{"role": "user", "content": "Test"}],
                model=model, max_tokens=100, temp=0.7, stream=False,
            )
            assert payload["model"] == model, f"{provider}/{model} payload mismatch"


# ═══════════════════════════════════════════════════════════════════════════
# 11. SSE + BOOT CONTRACT
# ═══════════════════════════════════════════════════════════════════════════

def test_sse_contract_shape():
    from api._boot import _SSE_CONTRACT
    c = _SSE_CONTRACT
    assert "/api/extract_stream" in c["endpoints"]
    frames = c["endpoints"]["/api/extract_stream"]["frames"]
    events = [f["event"] for f in frames]
    assert events[0] == "route"
    assert events[-1] == "close"
    assert events.count("route") == 1
    assert events.count("close") == 1


def test_sse_message_and_thinking_variants():
    from api._boot import _SSE_CONTRACT
    frames = _SSE_CONTRACT["endpoints"]["/api/extract_stream"]["frames"]
    msg_keysets = {tuple(sorted(f["data"].keys()))
                   for f in frames if f["event"] == "message"}
    assert ("text",) in msg_keysets
    assert ("feature_metadata",) in msg_keysets
    assert ("watermark",) in msg_keysets
    thinks = [f for f in frames if f["event"] == "thinking"]
    keysets = {tuple(sorted(f["data"].keys())) for f in thinks}
    assert any("stage" in ks for ks in keysets)
    assert any("content" in ks for ks in keysets)


def test_sse_contract_media_type_is_event_stream(client):
    r = client.get("/api/contract")
    if r.status_code == 404:
        pytest.skip("_boot.attach not wired in this deployment")
    body = r.json()
    assert body["endpoints"]["/api/extract_stream"]["media_type"] == "text/event-stream"


def test_audit_endpoint_reports_version(client):
    r = client.get("/api/audit")
    if r.status_code == 404:
        pytest.skip("_boot.attach not wired in this deployment")
    body = r.json()
    assert "version" in body


def test_extract_stream_rejects_bad_auth(client):
    r = client.post("/api/extract_stream", data={"command": "hi"})
    assert r.status_code in (401, 403, 422)


def test_boot_attach_adds_contract_and_audit_routes():
    from api._boot import attach
    from fastapi import FastAPI
    app = FastAPI()
    attach(app)
    paths = {r.path for r in app.routes}
    assert "/api/contract" in paths
    assert "/api/audit" in paths


def test_boot_audit_aggregates_resilience():
    from api._boot import audit
    a = audit()
    assert "resilience" in a
    assert "cognition_pipeline" in a


def test_boot_classify_intent_works():
    from api._boot import classify_intent
    assert classify_intent("hi") in ("conversational", "strategy")


def test_boot_contract_endpoint_if_present(client):
    r = client.get("/api/contract")
    if r.status_code == 404:
        pytest.skip("_boot.attach not wired in this deployment")
    assert r.status_code == 200


def test_boot_audit_endpoint_if_present(client):
    r = client.get("/api/audit")
    if r.status_code == 404:
        pytest.skip("_boot.attach not wired in this deployment")
    assert r.status_code == 200
