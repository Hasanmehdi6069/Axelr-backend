# api/routes_core.py
"""
AXELR — Core AI routes.
=======================
Primary chat/extract path plus the canonical AI router:

  * route_ai_request_parallel / sequential — the canonical router
  * stream_ai_response
  * /api/chat, /api/extract, /api/extract_stream
  * /api/guest/* (guest sessions + extraction)

Sibling modules:
  * api/prompts.py       — prompt construction + input/output sanitizers
  * api/quota.py         — tier config, quota, rate limiting
  * api/routes_tools.py  — tooling surface (tools, deploy, touch-fix, …)

Backwards compatibility
-----------------------
Every symbol that was previously importable from this module remains so.
route_ai_request_parallel / route_ai_request_sequential / stream_ai_response
continue to be defined here (api.middleware and api.routes_agents import them
from this path).
"""

import asyncio
import base64
import contextlib
import csv
import hashlib
import io
import json
import random
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .auth import get_current_user
from .config import (
    GROQ_API_KEY,
    GROQ_MODELS,
    WORKSPACE_LLM_CONFIG,
)
# Re-exported for backward compatibility with earlier imports of routes_core.
from .prompts import (
    # public helpers
    detect_manipulation,
    contains_explicit,
    sanitize_input,
    sanitize_ai_output,
    strip_system_prompt,
    strip_fluff,
    strip_system_prompt_sequential,
    _get_system_prompt,
    _SYSTEM_PROMPTS,
    _WORKSPACE_ADDENDA,
)
from .quota import (
    TIER_CONFIG,
    check_and_update_quota,
    check_rate_limit,
    estimate_tokens,
)
from .providers import (
    PROVIDER_FUNC_MAP,
    PROVIDER_KEY_CHECK,
    PROVIDER_MODELS,
    build_local_fallback_response,
    get_dynamically_ranked_providers,
    get_provider_order,
    record_provider_result,
)
from .state import (
    delete_redis_cache,
    get_object_id,
    get_redis_cache,
    limiter,
    set_redis_cache,
    state,
)

import structlog
logger = structlog.get_logger("axelr.core")

router = APIRouter(tags=["core"])

# api/routes_core.py — add near the guarded imports
import os as _os
from .config import HTTP_CLIENT as _HTTP_CLIENT

_SNAPDEPLOY_URL = (_os.getenv("SNAPDEPLOY_INFERENCE_URL") or "").strip().rstrip("/")
_SNAPDEPLOY_KEY = (_os.getenv("SNAPDEPLOY_INFERENCE_KEY") or "").strip()


async def execute_axelr_hot_swap_engine(
    workspace: str, task_type: str, prompt: str, tier: str = "free",
) -> str:
    """Cascade step [5] — delegate local inference to SnapDeploy."""
    if not _SNAPDEPLOY_URL:
        return ""

    headers = {"Content-Type": "application/json"}
    if _SNAPDEPLOY_KEY:
        headers["Authorization"] = f"Bearer {_SNAPDEPLOY_KEY}"

    try:
        r = await _HTTP_CLIENT.post(
            f"{_SNAPDEPLOY_URL}/infer",
            headers=headers,
            json={
                "workspace": workspace,
                "task_type": task_type,
                "prompt": prompt[:4000],
                "tier": tier,
            },
            timeout=25.0,
        )
        if r.status_code == 200:
            return (r.json().get("text") or "").strip()
    except Exception as e:
        logger.warning("snapdeploy_inference_failed error=%s", e)
    return ""


def get_hot_swap_engine():
    """Stub on Render — the real engine lives on SnapDeploy."""
    class _Stub:
        @staticmethod
        def snapshot() -> dict:
            return {"resident": "remote:snapdeploy"}
    return _Stub()
# ═══════════════════════════════════════════════════════════════════════════
# Optional heavyweight dependencies (guarded)
# ═══════════════════════════════════════════════════════════════════════════

try:
    from core import get_vector_cache, get_router, CodeGuard
    _semantic_cache = get_vector_cache()
    state.semantic_cache = _semantic_cache
    state.code_guard = CodeGuard()
    state.intent_router = get_router()
    logger.info("core_package_loaded")
except Exception as _e:                                       # noqa: BLE001
    logger.warning("core_package_missing_using_stubs error=%s", _e)

    class _NoCache:
        async def get(self, *a, **kw): return None
        async def set(self, *a, **kw): return None
        async def clear(self): return None
        async def _ensure_model(self): return None
    _semantic_cache = _NoCache()
    state.semantic_cache = _semantic_cache

    class _StubCodeGuard:
        def scan(self, code, language="python"):
            class _R:
                syntax_ok = True
                score = 100
                findings = []
            return _R()
    state.code_guard = _StubCodeGuard()

    class _StubRouter:
        async def classify(self, *a, **kw):
            class R:
                workspace = "core"
            return R()
    state.intent_router = _StubRouter()

try:
    from core.worker_client import execute_code_on_worker
except Exception:
    async def execute_code_on_worker(language, code, timeout=8):
        return {"success": False, "output": "", "error": "worker_unavailable"}

# ═══════════════════════════════════════════════════════════════════════════
# Local hot-swap inference engine (guarded — degrades to stub if unavailable)
# ═══════════════════════════════════════════════════════════════════════════

try:
    from core.hot_swap_engine import (
        execute_axelr_hot_swap_engine,
        get_hot_swap_engine,
    )
except Exception as _e:                                       # noqa: BLE001
    logger.warning("hot_swap_engine_missing_using_stub error=%s", _e)

    async def execute_axelr_hot_swap_engine(
        workspace: str, task_type: str, prompt: str, tier: str = "free",
    ) -> str:
        return ""

    class _StubEngine:
        @staticmethod
        def snapshot() -> dict:
            return {"resident": "unavailable"}
        async def start(self) -> None: ...
        async def shutdown(self) -> None: ...

    def get_hot_swap_engine() -> _StubEngine:      # type: ignore[misc]
        return _StubEngine()
# ═══════════════════════════════════════════════════════════════════════════
# Provider routing
# ═══════════════════════════════════════════════════════════════════════════

async def route_ai_request_sequential(
    workspace: str, task_type: str, prompt: str,
    history: list[dict] | None, files: list[dict] | None,
    max_tokens: int, temp: float, tier: str,
    user: dict | None = None, context: str = "",
) -> dict[str, Any]:
    start = time.time()
    prompt = sanitize_input(prompt)
    if detect_manipulation(prompt) or contains_explicit(prompt):
        return {"success": False, "text": "⚠️ Security violation.",
                "provider": "security", "model_used": "filter",
                "tokens_used": 0, "latency_ms": 0}

    history_text = ""
    if history:
        recent = []
        for m in history[-4:]:
            if not isinstance(m, dict):
                continue
            role = m.get("role", "user")
            content = m.get("content") or m.get("text") or ""
            if isinstance(content, list):
                content = "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
            if isinstance(content, str) and content.strip():
                recent.append(f"{role}: {content.strip()}")
        history_text = "\n".join(recent)
    system_prompt = _get_system_prompt(workspace, task_type)
    full_prompt = f"{system_prompt}\n\n"
    if context:
        full_prompt += f"Context: {context}\n\n"
    if history_text:
        full_prompt += f"Previous conversation:\n{history_text}\n\n"
    full_prompt += f"User request: {prompt}"

    normalized = " ".join(prompt.lower().split())
    ctx_hash = hashlib.sha256(context.encode()).hexdigest() if context else ""
    cache_key = hashlib.sha256(
        f"{workspace}:{task_type}:{normalized}:{history_text}:{ctx_hash}".encode()
    ).hexdigest()
    if cache_key in state.ai_cache:
        return {**state.ai_cache[cache_key], "cached": True}

    response_text = None
    provider_used = None
    model_used = None
    last_error = None

    provider_order = get_provider_order(workspace)
    _tier_cfg = TIER_CONFIG.get(tier, TIER_CONFIG["free"])
    _allowed = _tier_cfg.get("providers", "*")

    for provider_name in provider_order:
        if provider_name == "local":
            continue
        if _allowed != "*" and provider_name not in _allowed:
            continue
        func = PROVIDER_FUNC_MAP.get(provider_name)
        if not func or not PROVIDER_KEY_CHECK.get(provider_name, False):
            continue
        if state.circuit_breaker is not None and not await state.circuit_breaker.is_available(provider_name):
            continue

        models = PROVIDER_MODELS.get(provider_name, [])
        if not models:
            continue

        provider_success = False
        for model in models:
            ident = f"{provider_name}:{model}"
            if state.circuit_breaker is not None and not await state.circuit_breaker.is_available(ident):
                continue

            for attempt in range(2):
                try:
                    resp_text = await func(full_prompt, max_tokens, temp, model)
                    if resp_text:
                        response_text = resp_text
                        provider_used = provider_name
                        model_used = model
                        provider_success = True
                        if state.circuit_breaker is not None:
                            await state.circuit_breaker.record_success(provider_name)
                            await state.circuit_breaker.record_success(ident)
                        break
                except Exception as e:
                    last_error = e
                    msg = str(e).lower()
                    if "quota" in msg or "429" in msg or "payment required" in msg or "402" in msg:
                        if state.circuit_breaker is not None:
                            await state.circuit_breaker.record_failure(ident, is_rate_limit=True)
                        break
                    logger.warning("provider_attempt_failed provider=%s model=%s attempt=%s error=%s",
                                   provider_name, model, attempt + 1, e)
                    await asyncio.sleep(2 ** attempt)
                    if state.circuit_breaker is not None:
                        await state.circuit_breaker.record_failure(ident)
            if provider_success:
                break

        if provider_success:
            break
        if state.circuit_breaker is not None:
            await state.circuit_breaker.record_failure(provider_name)
                # ── All remote providers exhausted → local hot-swap ──────────────────
        # ── All remote providers exhausted → local hot-swap ──────────────────
    fallback_text = await execute_axelr_hot_swap_engine(
        workspace, task_type, prompt, tier=tier,
    )
    return {
        "success": True,
        "text": sanitize_ai_output(fallback_text),
        "provider": "local_hot_swap",
        "model_used": get_hot_swap_engine().snapshot().get("resident") or "stub",
        "tokens_used": len(fallback_text.split()),
        "latency_ms": round((time.time() - start) * 1000, 2),
    }

    response_text = sanitize_ai_output(response_text)
    elapsed = time.time() - start
    response_text += f"\n\n---\n*Generated through Axelr in {elapsed:.2f} seconds*"
    result = {
        "success": True, "text": response_text,
        "provider": provider_used, "model_used": model_used,
        "tokens_used": len(response_text.split()),
        "latency_ms": round(elapsed * 1000, 2),
    }
    state.ai_cache[cache_key] = result
    return result

async def route_ai_request_parallel(
    workspace: str, task_type: str, prompt: str,
    history: list[dict] | None, files: list[dict] | None,
    max_tokens: int, temp: float, tier: str,
    user: dict | None = None, context: str = "", request: Request | None = None,
) -> dict[str, Any]:
    await check_and_update_quota(user, workspace, task_type)
    start = time.time()

    prompt = sanitize_input(prompt)
    if detect_manipulation(prompt) or contains_explicit(prompt):
        return {"success": False, "text": "⚠️ Security violation.",
                "provider": "security", "model_used": "filter",
                "tokens_used": 0, "latency_ms": 0}

    # semantic cache
    try:
        cached_response = await _semantic_cache.get(prompt)
    except Exception:
        cached_response = None
    if cached_response:
        return {
            "success": True, "text": cached_response,
            "provider": "semantic_cache", "model_used": "cache",
            "tokens_used": len(cached_response.split()),
            "latency_ms": round((time.time() - start) * 1000, 2),
            "cached": True,
        }

    system_prompt = _get_system_prompt(workspace, task_type)
    full_prompt = f"{system_prompt}\n\n"
    if context:
        full_prompt += f"Context: {context}\n\n"
    if history:
        recent = [
            f"{m.get('role', 'user')}: {m.get('text', '')}"
            for m in history[-4:]
            if isinstance(m, dict) and m.get("text")
        ]
        if recent:
            full_prompt += "Previous conversation:\n" + "\n".join(recent) + "\n\n"
    full_prompt += f"User request: {prompt}"

    # ── Everything below stays INSIDE the function ────────────────────────
    _allowed = TIER_CONFIG.get(tier, TIER_CONFIG["free"]).get("providers", "*")

    ranked = get_dynamically_ranked_providers(workspace)
    available: list[str] = []
    for p in ranked:
        if p == "local":
            continue
        if _allowed != "*" and p not in _allowed:
            continue
        if state.circuit_breaker is None or await state.circuit_breaker.is_available(p):
            available.append(p)

    # NOTE: this check must be OUTSIDE the loop, not inside it.
    top_3 = available[:3]
    if not top_3:
        return await route_ai_request_sequential(
            workspace, task_type, prompt, history, files,
            max_tokens, temp, tier, user, context,
        )

    async def execute(p_name: str):
        t0 = time.time()
        func = PROVIDER_FUNC_MAP.get(p_name)
        models = PROVIDER_MODELS.get(p_name, [])
        model = models[0] if models else None
        try:
            resp = await func(full_prompt, max_tokens, temp, model)
            elapsed = time.time() - t0
            if resp and len(resp.strip()) > 10:
                record_provider_result(p_name, elapsed, success=True)
                return {"text": resp, "provider": p_name,
                        "model": model, "latency": elapsed}
            raise ValueError("Empty output")
        except Exception:
            record_provider_result(p_name, time.time() - t0, success=False)
            raise

    tasks = [asyncio.create_task(execute(p), name=f"race:{p}") for p in top_3]
    try:
        done, pending = await asyncio.wait(
            tasks, timeout=4.0, return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        for t in done:
            try:
                res = t.result()
            except Exception:
                continue
            final_text = sanitize_ai_output(res["text"]) + \
                f"\n\n---\n*Generated through Axelr in {res['latency']:.2f} seconds*"
            with contextlib.suppress(Exception):
                await _semantic_cache.set(prompt, final_text)
            return {
                "success": True, "text": final_text,
                "provider": res["provider"], "model_used": res["model"],
                "tokens_used": len(final_text.split()),
                "latency_ms": round(res["latency"] * 1000, 2),
            }
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("parallel_race_failed error=%s", e)
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()

    # jittered sequential fallback
    remaining = [p for p in available[3:] if p != "local"]
    delays = [0.5, 1.0, 2.0]
    for idx, p_name in enumerate(remaining):
        jitter = random.uniform(0.05, 0.25)
        await asyncio.sleep(delays[min(idx, len(delays) - 1)] + jitter)

        func = PROVIDER_FUNC_MAP.get(p_name)
        model = PROVIDER_MODELS.get(p_name, [None])[0]
        t0 = time.time()
        try:
            resp = await func(full_prompt, max_tokens, temp, model)
            elapsed = time.time() - t0
            if resp and len(resp.strip()) > 5:
                record_provider_result(p_name, elapsed, success=True)
                if state.circuit_breaker is not None:
                    await state.circuit_breaker.record_success(p_name)
                final_text = sanitize_ai_output(resp) + \
                    f"\n\n---\n*Generated through Axelr in {elapsed:.2f} seconds*"
                with contextlib.suppress(Exception):
                    await _semantic_cache.set(prompt, final_text)
                return {
                    "success": True, "text": final_text,
                    "provider": p_name, "model_used": model,
                    "tokens_used": len(final_text.split()),
                    "latency_ms": round(elapsed * 1000, 2),
                }
        except Exception:
            record_provider_result(p_name, time.time() - t0, success=False)
            if state.circuit_breaker is not None:
                await state.circuit_breaker.record_failure(p_name)
            continue

    fallback = build_local_fallback_response(workspace, "core", prompt)
    return {
        "success": True, "text": fallback,
        "provider": "local", "model_used": "local-fallback",
        "tokens_used": len(fallback.split()),
        "latency_ms": round((time.time() - start) * 1000, 2),
            # Locate this section at the bottom of route_ai_request_parallel within api/routes_core.py
    # Replace the existing static local fallback with our dynamic execution loop:
    
    fallback = await execute_axelr_hot_swap_engine(workspace, task_type, prompt)
    return {
        "success": True, 
        "text": fallback,
        "provider": "local_hot_swap", 
        "model_used": "tiny-llm-gateway-core",
        "tokens_used": len(fallback.split()),
        "latency_ms": round((time.time() - start) * 1000, 2),
    }

    }

# ═══════════════════════════════════════════════════════════════════════════
# File / workspace helpers
# ═══════════════════════════════════════════════════════════════════════════

def detect_workspace(command: str, files: list[dict]) -> str:
    if files:
        for f in files:
            fn = f.get("filename", "").lower()
            mt = f.get("mimetype", "").lower()
            if mt.startswith("image/") or fn.endswith((".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".webp")):
                return "design"
            if fn.endswith((".csv", ".xls", ".xlsx", ".pdf")) or "spreadsheet" in mt or "csv" in mt:
                return "data"
            if fn.endswith((".html", ".css", ".js", ".jsx", ".tsx", ".vue", ".py", ".java",
                           ".cpp", ".c", ".go", ".rs", ".rb", ".php", ".swift", ".kt")):
                return "design"
    if command:
        low = command.lower()
        if any(k in low for k in ("design", "ui", "ux", "mockup", "wireframe", "frontend",
                                  "html", "css", "react", "vue", "component", "interface", "prototype")):
            return "design"
        if any(k in low for k in ("extract", "analyze", "data", "csv", "table", "spreadsheet",
                                  "chart", "stats", "invoice", "receipt", "excel", "sheet",
                                  "tabular", "pivot", "aggregate")):
            return "data"
    return "core"


def is_allowed_file(workspace: str, filename: str, content_type: str) -> bool:
    if workspace == "data":
        allowed_types = ("image/", "application/pdf", "text/csv", "text/plain",
                         "application/vnd.ms-excel",
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         "application/msword",
                         "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        exts = (".csv", ".xls", ".xlsx", ".pdf", ".png", ".jpg", ".jpeg",
                ".gif", ".bmp", ".txt", ".doc", ".docx")
        return content_type.startswith(allowed_types) or filename.lower().endswith(exts)
    if workspace == "design":
        allowed_types = ("image/", "text/", "application/javascript", "application/json",
                         "application/xhtml+xml", "application/xml", "text/x-")
        exts = (".html", ".htm", ".css", ".js", ".ts", ".jsx", ".tsx", ".vue", ".svelte",
                ".py", ".ipynb", ".java", ".c", ".cpp", ".h", ".hpp", ".go", ".rs",
                ".rb", ".php", ".swift", ".kt", ".scala", ".hs", ".lua", ".pl", ".r",
                ".sh", ".bash", ".zsh", ".json", ".yaml", ".yml", ".toml", ".ini",
                ".md", ".markdown", ".txt", ".xml", ".svg", ".wasm",
                ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")
        return content_type.startswith(allowed_types) or filename.lower().endswith(exts)
    return True


async def discover_schema(files: list[dict]) -> str | None:
    try:
        import openpyxl
    except ImportError:
        openpyxl = None

    for f in files:
        fn = f.get("filename", "").lower()
        mt = f.get("mimetype", "").lower()
        b64 = f.get("content_base64", "")
        if not b64:
            continue

        if fn.endswith(".csv") or "csv" in mt:
            try:
                content = base64.b64decode(b64).decode("utf-8", errors="ignore")
                reader = csv.reader(io.StringIO(content))
                headers = next(reader, [])
                if headers:
                    return f"CSV columns: {', '.join(headers)}"
            except Exception as e:
                logger.warning("csv_schema_discovery_failed error=%s", e)

        elif fn.endswith((".xls", ".xlsx")) or "spreadsheet" in mt:
            if openpyxl is None:
                continue
            try:
                content = base64.b64decode(b64)
                with io.BytesIO(content) as buf:
                    wb = openpyxl.load_workbook(buf, read_only=True)
                    sheet = wb.active
                    first_row = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True), None)
                    if first_row:
                        headers = [str(c) for c in first_row if c is not None]
                        return f"Excel columns: {', '.join(headers)}"
                    wb.close()
            except Exception as e:
                logger.warning("excel_schema_discovery_failed error=%s", e)
    return None


def generate_dependencies(code: str, language: str) -> str | None:
    if language == "python":
        imports = re.findall(r"^(?:from|import)\s+(\w+)", code, re.MULTILINE)
        return "\n".join(sorted(set(imports))) if imports else None
    if language in ("javascript", "typescript"):
        imports = re.findall(r'(?:require|import)\s*\(?\s*["\']([^"\']+)["\']', code)
        deps = {pkg: "*" for pkg in imports if not pkg.startswith(".")}
        return json.dumps({"dependencies": deps}, indent=2) if deps else None
    return None


_STOP_WORDS = {
    "the", "be", "to", "of", "and", "a", "in", "that", "have", "i", "it",
    "for", "not", "on", "with", "he", "as", "you", "do", "at", "this",
    "but", "his", "by", "from", "they", "we", "say", "her", "she", "or",
    "an", "will", "my", "one", "all", "would", "there", "their", "what",
    "so", "up", "out", "if", "about", "who", "get", "which", "go", "me",
    "when", "make", "can", "like", "time", "no", "just", "him", "know",
    "take", "people", "into", "year", "your", "good", "some", "could",
    "them", "see", "other", "than", "then", "now", "look", "only", "come",
}


def generate_chat_name(command: str, files: list[UploadFile] | None) -> str:
    if files:
        base = files[0].filename.split(".")[0]
        return base.replace("_", " ").replace("-", " ")[:50] or "File Chat"
    if command and command.strip():
        words = command.strip().split()
        meaningful = [w for w in words if w.lower() not in _STOP_WORDS and len(w) > 2]
        if meaningful:
            return " ".join(meaningful[:3])[:60]
        return " ".join(words[:3])[:60]
    return f"Chat_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"


# ═══════════════════════════════════════════════════════════════════════════
# Sandbox adapter
# ═══════════════════════════════════════════════════════════════════════════

async def _sandbox_execute(language: str, code: str, timeout: int = 8) -> dict[str, Any]:
    try:
        return await execute_code_on_worker(
            language, code, max(1, min(int(timeout or 8), 10)),
        )
    except Exception as e:
        logger.warning("sandbox_execute_failed error=%s", e)
        return {"success": False, "output": "", "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# Streaming
# ═══════════════════════════════════════════════════════════════════════════

async def stream_ai_response(
    workspace: str, task_type: str, prompt: str,
    history: list[dict] | None, files: list[dict] | None,
    max_tokens: int, temp: float, tier: str,
    user: dict | None = None, context: str = "",
):
    start = time.time()

    system_prompt = _get_system_prompt(workspace, task_type)
    full_prompt = f"{system_prompt}\n\n"
    if context:
        full_prompt += f"Context: {context}\n\n"
    if history:
        recent = [
            f"{m.get('role', 'user')}: {m.get('text', '')}"
            for m in history[-4:]
            if isinstance(m, dict) and m.get("text")
        ]
        if recent:
            full_prompt += "Previous conversation:\n" + "\n".join(recent) + "\n\n"
    full_prompt += f"User request: {prompt}"

    # 1. semantic cache
    try:
        cached_response = await _semantic_cache.get(prompt)
    except Exception:
        cached_response = None
    if cached_response:
        for word in cached_response.split():
            yield f"data: {json.dumps({'text': word + ' '})}\n\n"
            await asyncio.sleep(0.005)
        cache_ms = (time.time() - start) * 1000
        wm = f"\n\n---\n*Served from Axelr Vector Cache in {cache_ms:.1f}ms*"
        yield f"data: {json.dumps({'watermark': wm})}\n\n"
        return

    # 2. native Groq streaming
    if GROQ_API_KEY:
        try:
            url = "https://api.groq.com/openai/v1/chat/completions"
            headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
            payload = {
                "model": GROQ_MODELS[0] if GROQ_MODELS else "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": full_prompt}],
                "max_tokens": max_tokens, "temperature": temp, "stream": True,
            }
            async with httpx.AsyncClient(timeout=20.0) as client:
                async with client.stream("POST", url, headers=headers, json=payload) as response:
                    if response.status_code == 200:
                        collected: list[str] = []
                        async for line in response.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            data_str = line[6:].strip()
                            if data_str == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data_str)
                                delta = (chunk.get("choices", [{}])[0]
                                         .get("delta", {}).get("content", ""))
                                if delta:
                                    collected.append(delta)
                                    yield f"data: {json.dumps({'text': delta})}\n\n"
                            except Exception:
                                continue

                        full_res = sanitize_ai_output("".join(collected))
                        with contextlib.suppress(Exception):
                            await _semantic_cache.set(prompt, full_res)
                        elapsed = time.time() - start
                        wm = f"\n\n---\n*Streamed through Axelr in {elapsed:.2f} seconds*"
                        yield f"data: {json.dumps({'watermark': wm})}\n\n"
                        return
        except Exception as e:
            logger.warning("groq_stream_failed error=%s", e)

    # 3. sequential fallback
    result = await route_ai_request_sequential(
        workspace, task_type, prompt, history, files,
        max_tokens, temp, tier, user, context,
    )
    if not result.get("success"):
        yield f"data: {json.dumps({'error': result.get('text', 'AI unavailable')})}\n\n"
        return

    full_text = sanitize_ai_output(result["text"])
    buf: list[str] = []
    tokens = full_text.split()
    for i, w in enumerate(tokens):
        buf.append(w)
        if len(buf) >= 8 or i == len(tokens) - 1:
            yield f"data: {json.dumps({'text': ' '.join(buf) + ' '})}\n\n"
            buf.clear()
            await asyncio.sleep(0)

    watermark = f"\n\n---\n*Generated through Axelr in {time.time() - start:.2f} seconds*"
    yield f"data: {json.dumps({'watermark': watermark})}\n\n"


# ═══════════════════════════════════════════════════════════════════════════
# Endpoints — chat / extract
# ═══════════════════════════════════════════════════════════════════════════

class ChatRequestBody(BaseModel):
    command: str
    workspace: str | None = None
    sessionId: str | None = None
    context: str | None = None
    provider: str = "gemini"
    max_tokens: int | None = None
    temperature: float | None = None


@router.post("/api/chat", tags=["AI"])
async def api_chat_main(body: ChatRequestBody, request: Request):
    return await route_ai_request_parallel(
        workspace=body.workspace or "core",
        task_type="structuring",
        prompt=body.command,
        history=None, files=None,
        max_tokens=body.max_tokens or 2048,
        temp=body.temperature if body.temperature is not None else 0.4,
        tier="free", user=None, context=body.context or "", request=request,
    )


async def _persist_chat_session(
    user: dict, session_id: str | None, workspace: str,
    command: str, full_response: str, file_contents: list[dict],
    project_id: str | None = None,
) -> None:
    if not state.db_available:
        return
    ObjectId = get_object_id()
    try:
        new_user_msg = {
            "role": "user", "text": command,
            "attachedFiles": [f["filename"] for f in file_contents],
            "createdAt": datetime.now(timezone.utc),
        }
        new_model_msg = {
            "role": "model", "text": full_response.strip(),
            "variants": [full_response.strip()], "activeVariant": 0,
            "canRegenerate": True, "createdAt": datetime.now(timezone.utc),
        }

        if session_id and ObjectId and ObjectId.is_valid(session_id):
            await state.sessions_col.update_one(
                {"_id": ObjectId(session_id), "userId": user["_id"]},
                {"$push": {"messages": {"$each": [new_user_msg, new_model_msg]}}},
            )
            return

        filename = generate_chat_name(command, file_contents)
        new_session = {
            "userId": user["_id"], "filename": filename,
            "workspace": workspace, "status": "active", "isPinned": False,
            "messages": [new_user_msg, new_model_msg],
            "createdAt": datetime.utcnow(),
        }
        if project_id and ObjectId and ObjectId.is_valid(project_id):
            new_session["projectId"] = ObjectId(project_id)
        await state.sessions_col.insert_one(new_session)
    except Exception as e:
        logger.error("chat_session_persist_failed error=%s", e)


@router.post("/api/extract_stream")
@limiter.limit("5/minute")
async def extract_stream(
    request: Request,
    user: dict = Depends(get_current_user),
    command: str = Form(...),
    workspace: str | None = Form(None),
    task_type: str | None = Form(None),
    sessionId: str | None = Form(None),
    projectId: str | None = Form(None),
    context: str | None = Form(None),
    files: list[UploadFile] = File([]),
):
    allowed, reset_sec = await check_rate_limit(str(user["_id"]), user.get("tier", "free"), "extract_stream")
    if not allowed:
        raise HTTPException(status_code=429, detail={
            "code": "QUOTA_EXCEEDED",
            "message": f"Rate limit exceeded. Try again in {reset_sec} seconds.",
            "reset": reset_sec,
        })
    if not state.db_available:
        raise HTTPException(status_code=503, detail="Database unavailable")

    file_infos = [{"filename": f.filename, "mimetype": f.content_type or ""} for f in files]
    workspace = workspace or detect_workspace(command, file_infos)
    if workspace not in ("data", "design", "core"):
        workspace = "core"

    valid_files = [f for f in files if is_allowed_file(workspace, f.filename, f.content_type or "")]

    file_contents = []
    for f in valid_files:
        content_bytes = await f.read()
        file_contents.append({
            "filename": f.filename,
            "mimetype": f.content_type or "application/octet-stream",
            "content_base64": base64.b64encode(content_bytes).decode("utf-8"),
        })

    if task_type is None:
        task_type = {"data": "extraction", "design": "frontend"}.get(workspace, "structuring")
    if task_type not in ("extraction", "frontend", "structuring", "touch_fix"):
        task_type = "extraction" if workspace == "data" else "frontend"

    ObjectId = get_object_id()
    if sessionId and (not ObjectId or not ObjectId.is_valid(sessionId)):
        sessionId = None
    history = []
    if sessionId and ObjectId:
        session = await state.sessions_col.find_one(
            {"_id": ObjectId(sessionId), "userId": user["_id"]}
        )
        if session:
            history = session.get("messages", [])

    combined_context = context or ""
    if state.context_registry and state.db_available:
        with contextlib.suppress(Exception):
            reg_context = await state.context_registry.get_context(user["_id"], workspace) or ""
            combined_context += f"\n{reg_context}"

    schema_info = await discover_schema(file_contents)
    if schema_info:
        combined_context += f"\nSchema info: {schema_info}\n"

    llm_config = WORKSPACE_LLM_CONFIG.get(workspace, WORKSPACE_LLM_CONFIG["core"])
    max_tokens = llm_config["max_tokens"]
    temp = llm_config["temperature"]

    full_response = ""

    async def event_generator():
        nonlocal full_response
        sanitized = sanitize_input(command)
        async for event in stream_ai_response(
            workspace=workspace, task_type=task_type, prompt=sanitized,
            history=history, files=file_contents,
            max_tokens=max_tokens, temp=temp,
            tier=user.get("tier", "free"), user=user, context=combined_context,
        ):
            if "data: " in event and '"text":' in event:
                with contextlib.suppress(Exception):
                    payload = json.loads(event.split("data: ", 1)[1].strip())
                    if "text" in payload:
                        full_response += payload["text"]
            yield event

        await _persist_chat_session(
            user=user, session_id=sessionId, workspace=workspace,
            command=command, full_response=full_response,
            file_contents=file_contents, project_id=projectId,
        )
        yield "event: close\ndata: {}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/api/extract")
@limiter.limit("100/minute")
async def extract(
    request: Request,
    user: dict = Depends(get_current_user),
    command: str = Form(...),
    workspace: str | None = Form(None),
    task_type: str | None = Form(None),
    isRetry: str = Form("false"),
    sessionId: str | None = Form(None),
    projectId: str | None = Form(None),
    files: list[UploadFile] = File([]),
):
    allowed, reset_sec = await check_rate_limit(str(user["_id"]), user.get("tier", "free"), "extract")
    if not allowed:
        raise HTTPException(status_code=429, detail={
            "code": "QUOTA_EXCEEDED",
            "message": f"Rate limit exceeded. Try again in {reset_sec} seconds.",
            "reset": reset_sec,
        })
    if not state.db_available:
        raise HTTPException(status_code=503, detail="Database unavailable")

    # workspace + file validation
    file_infos = [{"filename": f.filename, "mimetype": f.content_type or ""} for f in files]
    workspace = workspace or detect_workspace(command, file_infos)
    if workspace not in ("data", "design", "core"):
        workspace = "core"

    if len(files) > 5:
        raise HTTPException(status_code=400, detail="Too many files (max 5)")

    valid_files = []
    rejected = []
    for f in files:
        if is_allowed_file(workspace, f.filename, f.content_type or ""):
            valid_files.append(f)
        else:
            rejected.append(f.filename)
    if rejected:
        raise HTTPException(status_code=400, detail=f"Unsupported file type(s) for {workspace}: {', '.join(rejected)}")
    files = valid_files

    total_size = 0
    for f in files:
        size = f.size or 0
        if size > 10 * 1024 * 1024:
            raise HTTPException(status_code=400, detail=f"File {f.filename} exceeds 10MB")
        total_size += size
    if total_size > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Total upload size too large")

    tier = user.get("tier", "free")
    is_design = workspace == "design"

    # tier gating
    if tier == "free":
        data_limit, ui_limit = 5, 3
    elif tier == "pro":
        has_data = user.get("subTierOptions", {}).get("hasDataAccess", False)
        has_design = user.get("subTierOptions", {}).get("hasDesignAccess", False)
        if has_data and has_design:
            data_limit, ui_limit = 20, 15
        elif has_data:
            data_limit, ui_limit = 19, 0
        elif has_design:
            data_limit, ui_limit = 0, 13
        else:
            data_limit, ui_limit = 0, 0
    elif tier == "business":
        has_data = user.get("subTierOptions", {}).get("hasDataAccess", False)
        has_design = user.get("subTierOptions", {}).get("hasDesignAccess", False)
        if has_data and has_design:
            data_limit, ui_limit = 30, 25
        elif has_data:
            data_limit, ui_limit = 28, 0
        elif has_design:
            data_limit, ui_limit = 0, 20
        else:
            data_limit, ui_limit = 0, 0
    else:
        data_limit, ui_limit = 5, 0

    limit = ui_limit if is_design else data_limit
    quota_field = "quotas.dailyGenerationsUsed" if is_design else "quotas.dailyExtractionsUsed"
    parts = quota_field.split(".")
    current = user.get(parts[0], {}).get(parts[1], 0)
    if current > limit:
        await state.users_col.update_one({"_id": user["_id"]}, {"$set": {quota_field: limit}})
        current = limit
    if current >= limit:
        raise HTTPException(status_code=403, detail={"code": "LIMIT_REACHED", "usage": current, "limit": limit})

    # read files
    file_contents = []
    for f in files:
        content_bytes = await f.read()
        file_contents.append({
            "filename": f.filename,
            "mimetype": f.content_type or "application/octet-stream",
            "content_base64": base64.b64encode(content_bytes).decode("utf-8"),
        })

    if task_type is None:
        task_type = {"data": "extraction", "design": "frontend"}.get(workspace, "structuring")
    if task_type not in ("extraction", "frontend", "structuring", "touch_fix"):
        task_type = "extraction" if workspace == "data" else "frontend"

    ObjectId = get_object_id()
    if sessionId and (not ObjectId or not ObjectId.is_valid(sessionId)):
        sessionId = None

    current_session = None
    history = []
    if sessionId and ObjectId:
        current_session = await state.sessions_col.find_one(
            {"_id": ObjectId(sessionId), "userId": user["_id"]}
        )
        if current_session:
            history = current_session.get("messages", [])
            if isRetry == "true" and history and history[-1].get("role") == "model":
                history = history[:-2]

    # intent classification
    if state.intent_classifier:
        with contextlib.suppress(Exception):
            intent_result = await state.intent_classifier.classify(command, file_contents)
            new_ws = getattr(intent_result, "workspace", None)
            if new_ws in ("data", "design", "core"):
                workspace = new_ws

    # context
    context = ""
    if state.context_registry and state.db_available:
        with contextlib.suppress(Exception):
            context = await state.context_registry.get_context(user["_id"], workspace) or ""

    schema_info = await discover_schema(file_contents)
    if schema_info:
        context += f"\nSchema info: {schema_info}\n"

    llm_config = WORKSPACE_LLM_CONFIG.get(workspace, WORKSPACE_LLM_CONFIG["core"])
    max_tokens = llm_config["max_tokens"]
    temp = llm_config["temperature"]

    ai_result = await route_ai_request_parallel(
        workspace=workspace, task_type=task_type, prompt=command,
        history=history, files=file_contents,
        max_tokens=max_tokens, temp=temp, tier=tier,
        user=user, context=context,
    )
    if not ai_result.get("success"):
        raise HTTPException(status_code=503, detail="AI service unavailable")

    ai_text = ai_result["text"]
    provider = ai_result.get("provider")
    model_used = ai_result.get("model_used")

    # critic + self-heal
    is_code = ("```" in ai_text) or any(ext in ai_text for ext in (".py", ".js", ".html", ".css"))
    if state.critic_agent and is_code and state.code_guard is not None:
        with contextlib.suppress(Exception):
            language = "python" if workspace == "data" else "javascript"
            scan = state.code_guard.scan(ai_text, language=language)
            if not (scan.syntax_ok and scan.score >= 60) and state.self_healer:
                heal = await state.self_healer.heal(
                    ai_text,
                    error=(scan.findings[0].message if scan.findings else "unknown"),
                    language=language, tier=tier, user=user,
                )
                if heal.success:
                    ai_text = heal.final_code

    # dependency resolver
    if is_code:
        deps = generate_dependencies(ai_text, "python" if workspace == "data" else "javascript")
        if deps:
            ai_text += f"\n\n**Generated Dependencies:**\n```\n{deps}\n```"

    # structured data
    structured: list = []
    json_match = re.search(r"\[JSON-DATA\](.*?)\[/JSON-DATA\]", ai_text, re.DOTALL)
    if json_match:
        with contextlib.suppress(Exception):
            structured = json.loads(json_match.group(1).strip())
        ai_text = re.sub(r"\[JSON-DATA\].*?\[/JSON-DATA\]", "", ai_text, flags=re.DOTALL).strip()

    if not ai_text:
        ai_text = "I am Axelr AI. How can I help you?"

    # quota + tokens
    if provider != "local":
        prompt_tokens = estimate_tokens(command) + (estimate_tokens(str(history)) if history else 0)
        completion_tokens = estimate_tokens(ai_text)
        update_query: dict[str, Any] = {
            "$inc": {
                "tokenUsage.totalPromptTokens": prompt_tokens,
                "tokenUsage.totalCompletionTokens": completion_tokens,
                "tokenUsage.dailyPromptTokens": prompt_tokens,
                "tokenUsage.dailyCompletionTokens": completion_tokens,
                quota_field: 1,
                "dailyUsage": 1,
                "storageBytesUsed": total_size,
            },
            "$set": {"lastUsageDate": datetime.utcnow()},
        }
        provider_map = {
            "gemini": "dailyGeminiQuota", "groq": "dailyGroqQuota",
            "cloudflare": "dailyCloudflareQuota", "openrouter": "dailyOpenRouterQuota",
            "mistral": "dailyMistralQuota", "huggingface": "dailyHuggingFaceQuota",
            "github_models": "dailyGithubQuota", "nrouter": "dailyNrouterQuota",
            "text_cortex": "dailyTextCortexQuota",
        }
        if provider in provider_map:
            update_query["$inc"][provider_map[provider]] = 1
        await state.users_col.update_one({"_id": user["_id"]}, update_query)

    # session persistence
    session_id_out = None
    filename_out = "Export"
    if current_session:
        if isRetry == "true" and current_session.get("messages"):
            last_msg = current_session["messages"][-1]
            if last_msg.get("role") == "model":
                variants = last_msg.get("variants") or [last_msg.get("text", "")]
                variants.append(ai_text)
                last_msg["variants"] = variants
                last_msg["activeVariant"] = len(variants) - 1
                last_msg["text"] = ai_text
                await state.sessions_col.update_one(
                    {"_id": ObjectId(sessionId)},
                    {"$set": {"messages": current_session["messages"], "structuredData": structured}},
                )
            else:
                raise HTTPException(status_code=400, detail="Cannot retry")
        else:
            current_session["messages"].append({
                "role": "user", "text": command,
                "attachedFiles": [f.filename for f in files],
            })
            current_session["messages"].append({
                "role": "model", "text": ai_text,
                "variants": [ai_text], "activeVariant": 0,
                "canRegenerate": True, "createdAt": datetime.utcnow(),
            })
            await state.sessions_col.update_one(
                {"_id": ObjectId(sessionId)},
                {"$set": {"messages": current_session["messages"], "structuredData": structured}},
            )
        session_id_out = sessionId
        filename_out = current_session.get("filename", "Export")
    else:
        filename = generate_chat_name(command, files)
        new_session = {
            "userId": user["_id"], "filename": filename,
            "workspace": workspace, "status": "active", "isPinned": False,
            "messages": [
                {"role": "user", "text": command,
                 "attachedFiles": [f.filename for f in files],
                 "createdAt": datetime.utcnow()},
                {"role": "model", "text": ai_text,
                 "variants": [ai_text], "activeVariant": 0,
                 "canRegenerate": True, "createdAt": datetime.utcnow()},
            ],
            "structuredData": structured,
            "createdAt": datetime.utcnow(),
        }
        if projectId and ObjectId and ObjectId.is_valid(projectId):
            new_session["projectId"] = ObjectId(projectId)
        result = await state.sessions_col.insert_one(new_session)
        session_id_out = str(result.inserted_id)
        filename_out = filename

    return {
        "success": True, "text": ai_text,
        "sessionId": session_id_out, "structuredData": structured,
        "filename": f"{filename_out}.csv",
        "provider": provider, "model": model_used,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Guest session endpoints
# ═══════════════════════════════════════════════════════════════════════════

class GuestSession(BaseModel):
    sessionId: str
    expiresIn: int


@router.post("/api/guest/session")
async def create_guest_session():
    session_id = secrets.token_urlsafe(16)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    await set_redis_cache(f"guest:{session_id}", {
        "expires": expires_at.isoformat(), "messages": [], "structured": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }, ttl=3600)
    return {"sessionId": session_id, "expiresIn": 3600}


@router.get("/api/guest/session/{session_id}")
async def get_guest_session(session_id: str):
    session = await get_redis_cache(f"guest:{session_id}")
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    expires_at = datetime.fromisoformat(session["expires"])
    if datetime.now(timezone.utc) > expires_at:
        await delete_redis_cache(f"guest:{session_id}")
        raise HTTPException(status_code=404, detail="Session expired")
    return {
        "sessionId": session_id,
        "expiresIn": int((expires_at - datetime.now(timezone.utc)).total_seconds()),
        "messageCount": len(session.get("messages", [])),
        "hasStructured": session.get("structured") is not None,
    }


@router.post("/api/guest/extract")
async def guest_extract(
    command: str = Form(...),
    workspace: str | None = Form(None),
    sessionId: str | None = Form(None),
    files: list[UploadFile] = File([]),
):
    file_infos = [{"filename": f.filename, "mimetype": f.content_type or ""} for f in files]
    workspace = workspace or detect_workspace(command, file_infos)
    if workspace not in ("data", "design", "core"):
        workspace = "core"

    if not sessionId or not await get_redis_cache(f"guest:{sessionId}"):
        session_id = secrets.token_urlsafe(16)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        await set_redis_cache(f"guest:{session_id}", {
            "expires": expires_at.isoformat(), "messages": [], "structured": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }, ttl=3600)
        sessionId = session_id

    session = await get_redis_cache(f"guest:{sessionId}")
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    expires_at = datetime.fromisoformat(session["expires"])
    if datetime.now(timezone.utc) > expires_at:
        await delete_redis_cache(f"guest:{sessionId}")
        raise HTTPException(status_code=403, detail="Session expired")
    if len(session.get("messages", [])) >= 5:
        raise HTTPException(status_code=403, detail={
            "code": "GUEST_LIMIT_REACHED",
            "message": "Guest sessions limited to 5 messages.",
            "limit": 5, "used": len(session.get("messages", [])),
        })

    valid = [f for f in files if is_allowed_file(workspace, f.filename, f.content_type or "")]
    file_contents = []
    for f in valid:
        content_bytes = await f.read()
        file_contents.append({
            "filename": f.filename,
            "mimetype": f.content_type or "application/octet-stream",
            "content_base64": base64.b64encode(content_bytes).decode("utf-8"),
        })

    context = ""
    schema_info = await discover_schema(file_contents)
    if schema_info:
        context = f"\nSchema info: {schema_info}\n"

    llm_config = WORKSPACE_LLM_CONFIG.get(workspace, WORKSPACE_LLM_CONFIG["core"])
    ai_result = await route_ai_request_parallel(
        workspace=workspace,
        task_type="extraction" if workspace == "data" else "frontend",
        prompt=command,
        history=session.get("messages", []),
        files=file_contents,
        max_tokens=llm_config["max_tokens"],
        temp=llm_config["temperature"],
        tier="free", user=None, context=context,
    )
    if not ai_result.get("success"):
        raise HTTPException(status_code=503, detail="AI service unavailable")

    ai_text = ai_result["text"]
    structured: list = []
    json_match = re.search(r"\[JSON-DATA\](.*?)\[/JSON-DATA\]", ai_text, re.DOTALL)
    if json_match:
        with contextlib.suppress(Exception):
            structured = json.loads(json_match.group(1).strip())
        ai_text = re.sub(r"\[JSON-DATA\].*?\[/JSON-DATA\]", "", ai_text, flags=re.DOTALL).strip()

    session["messages"].append({"role": "user", "text": command,
                                 "attachedFiles": [f.filename for f in valid]})
    session["messages"].append({"role": "model", "text": ai_text,
                                 "variants": [ai_text], "activeVariant": 0,
                                 "canRegenerate": True,
                                 "createdAt": datetime.utcnow().isoformat()})
    session["structured"] = structured

    remaining_ttl = int((expires_at - datetime.now(timezone.utc)).total_seconds())
    if remaining_ttl > 0:
        await set_redis_cache(f"guest:{sessionId}", session, ttl=remaining_ttl)

    remaining = max(0, 5 - (len(session["messages"]) // 2))
    return {
        "success": True, "text": ai_text, "sessionId": sessionId,
        "structuredData": structured,
        "filename": f"Export_{datetime.utcnow().strftime('%Y%m%d')}.csv",
        "provider": ai_result.get("provider"),
        "model": ai_result.get("model_used"),
        "remaining": remaining,
    }
# ═══════════════════════════════════════════════════════════════════════════
# Public surface
# ═══════════════════════════════════════════════════════════════════════════
__all__ = [
    "router",
    # core router (kept here — middleware/agents import from this path)
    "route_ai_request_parallel",
    "route_ai_request_sequential",
    "stream_ai_response",
    # local inference
    "execute_axelr_hot_swap_engine",
    "get_hot_swap_engine",
    # re-exported from api.prompts (backward compat)
    "detect_manipulation",
    "contains_explicit",
    "sanitize_input",
    "sanitize_ai_output",
    "strip_system_prompt",
    "strip_fluff",
    "strip_system_prompt_sequential",
    # re-exported from api.quota (backward compat)
    "check_and_update_quota",
    "check_rate_limit",
    "TIER_CONFIG",
    "estimate_tokens",
    # helpers that stayed put
    "generate_chat_name",
    "is_allowed_file",
    "detect_workspace",
    "discover_schema",
    "generate_dependencies",
    "_sandbox_execute",
]