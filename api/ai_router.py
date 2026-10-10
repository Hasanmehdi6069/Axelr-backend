# api/ai_router.py
"""
AXELR — Canonical AI Router
============================
The three routing entry points every other module depends on:

    route_ai_request_sequential  — provider-by-provider walk
    route_ai_request_parallel    — primary + parallel fallback race
    stream_ai_response           — SSE wrapper over core.resilience

Plus:
    generate_with_cascade        — cascade-aware wrapper (routes_core legacy)

Endpoints live in `api.routes_chat` and `api.routes_guest`. This module is
pure routing — no @router decorators.

Timing contract (this revision):
    primary provider has a 400 ms TTFT budget. If it doesn't answer in
    400 ms, the fallback providers fire in parallel. The whole request is
    capped at 15 s (free) / 30 s (pro) wall-clock.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import random
import time
from typing import Any

import structlog
from fastapi import Request
from .config import WORKSPACE_LLM_CONFIG
from .prompts import (
    PROMPT_VERSION,
    _get_system_prompt, contains_explicit, detect_manipulation,
    sanitize_ai_output, sanitize_input,
)
from .providers import (
    PROVIDER_FUNC_MAP, PROVIDER_KEY_CHECK, PROVIDER_MODELS,
    build_local_fallback_response, get_dynamically_ranked_providers,
    get_provider_order, record_provider_result,
)
from .quota import TIER_CONFIG, check_and_update_quota
from .state import state
logger = structlog.get_logger("axelr.ai_router")


# ═══════════════════════════════════════════════════════════════════════════
# Local inference bridge (guarded — degrades to stub)
# ═══════════════════════════════════════════════════════════════════════════
try:
    from core.twin_engine import (
        execute_axelr_hot_swap_engine,
        get_hot_swap_engine,
        get_twin_engine,
        get_twin_engine_async,
    )
except Exception as _e:
    if os.getenv("ENVIRONMENT", "development").lower() != "production":
        logger.warning("twin_engine_missing_using_stub error=%s", _e)

    async def execute_axelr_hot_swap_engine(*a, **kw) -> str:
        return ""

    async def get_twin_engine_async():
        return None

    def get_twin_engine():
        return None

    class _StubEngine:
        @staticmethod
        def snapshot() -> dict:
            return {"resident": "unavailable", "weights_loads": 0, "rss_mb": 0}
        async def start(self) -> None: ...
        async def shutdown(self) -> None: ...
        async def infer(self, *a, **kw) -> str: return ""

    def get_hot_swap_engine() -> _StubEngine:
        return _StubEngine()


def is_stub_response(text: str) -> bool:
    if not text:
        return True
    if text.lstrip().startswith("[stub]"):
        return True
    try:
        j = json.loads(text)
    except Exception:
        return False
    return isinstance(j, dict) and bool(j.get("_stub"))


# ═══════════════════════════════════════════════════════════════════════════
# Optional heavyweight deps (guarded)
# ═══════════════════════════════════════════════════════════════════════════
try:
    from core.code_intel import CodeGuard
    from core.cache import get_vector_cache

    _semantic_cache = get_vector_cache()
    state.semantic_cache = _semantic_cache
    state.code_guard = CodeGuard()
    logger.info("core_package_loaded")
except Exception as _e:
    if os.getenv("ENVIRONMENT", "development").lower() != "production":
        logger.warning("core_package_missing_using_stubs error=%s", _e)

    class _NoCache:
        async def get(self, *a, **kw): return None
        async def set(self, *a, **kw): return None
        async def clear(self): return None
        async def _ensure_model(self): return None
    _semantic_cache = _NoCache()
    state.semantic_cache = _semantic_cache

    class _StubCodeGuard:
        async def scan(self, code, language="python"):
            class _R:
                syntax_ok = True
                score = 100
                findings = []
            return _R()
    state.code_guard = _StubCodeGuard()


try:
    from core.io_pipeline import execute_code_on_worker
except Exception:
    async def execute_code_on_worker(language, code, timeout=8):
        return {"success": False, "output": "", "error": "worker_unavailable"}


# ═══════════════════════════════════════════════════════════════════════════
# Cascade wrapper
# ═══════════════════════════════════════════════════════════════════════════
async def generate_with_cascade(
    workspace: str, task_type: str, prompt: str,
    *, history=None, files=None, max_tokens=2048, temp=0.4,
    tier: str = "free", user: dict | None = None,
    context: str = "", user_id: str | None = None,
    session_id: str | None = None, want_elite: bool = False,
    request: Request | None = None,
) -> dict[str, Any]:
    cascade = getattr(state, "provider_cascade", None)
    if cascade is None:
        return await route_ai_request_parallel(
            workspace, task_type, prompt, history, files,
            max_tokens, temp, tier, user, context, request,
        )
    return await cascade.generate(
        workspace=workspace, task_type=task_type, prompt=prompt,
        user_id=user_id or (str(user["_id"]) if user else None),
        session_id=session_id, history=history, files=files,
        max_tokens=max_tokens, temp=temp, tier=tier, user=user,
        context=context, want_elite=want_elite,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Sequential router
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

    provider_order = get_provider_order(workspace)
    _allowed = TIER_CONFIG.get(tier, TIER_CONFIG["free"]).get("providers", "*")

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
                        if state.circuit_breaker is not None:
                            await state.circuit_breaker.record_success(provider_name)
                            await state.circuit_breaker.record_success(ident)
                        provider_success = True
                        break
                except Exception as e:
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

    fallback_text = await execute_axelr_hot_swap_engine(workspace, task_type, prompt)
    try:
        resident = get_hot_swap_engine().snapshot().get("resident") or "stub"
    except Exception:
        resident = "stub"
    provider = "local_stub" if is_stub_response(fallback_text) else "local_hot_swap"
    return {
        "success": True, "text": fallback_text, "provider": provider,
        "model_used": resident, "tokens_used": len(fallback_text.split()),
        "latency_ms": round((time.time() - start) * 1000, 2),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Parallel router (primary + parallel fallback race)
# ═══════════════════════════════════════════════════════════════════════════
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

    cache_context = ""
    if history and len(history) >= 2:
        recent_context = history[-2:]
        cache_context = "\n".join([
            f"{m.get('role', 'user')}: {m.get('text', '')}"
            for m in recent_context
            if isinstance(m, dict) and m.get("text")
        ])
    contextualized_prompt = f"{cache_context}\nCurrent request: {prompt}" if cache_context else prompt
    system_prompt = _get_system_prompt(workspace, task_type)
    cache_input = f"{PROMPT_VERSION}::{system_prompt}::{contextualized_prompt}"

    try:
        cached_response = await _semantic_cache.get(cache_input)
    except Exception:
        cached_response = None
    if cached_response:
        return {"success": True, "text": cached_response,
                "provider": "semantic_cache", "model_used": "cache",
                "tokens_used": len(cached_response.split()),
                "latency_ms": round((time.time() - start) * 1000, 2),
                "cached": True}
    full_prompt = f"{system_prompt}\n\n"
    if context:
        full_prompt += f"Context: {context}\n\n"
    if history:
        recent = [f"{m.get('role', 'user')}: {m.get('text', '')}"
                  for m in history[-4:] if isinstance(m, dict) and m.get("text")]
        if recent:
            full_prompt += "Previous conversation:\n" + "\n".join(recent) + "\n\n"
    full_prompt += f"User request: {prompt}"

    _allowed = TIER_CONFIG.get(tier, TIER_CONFIG["free"]).get("providers", "*")
    ranked = await get_dynamically_ranked_providers(workspace)
    available: list[str] = []
    for p in ranked:
        if p == "local":
            continue
        if _allowed != "*" and p not in _allowed:
            continue
        if state.circuit_breaker is None or await state.circuit_breaker.is_available(p):
            available.append(p)

    if not available:
        return await route_ai_request_sequential(
            workspace, task_type, prompt, history, files,
            max_tokens, temp, tier, user, context)

    primary_provider = available[0]
    fallback_providers = available[1:]

    async def call_provider_with_timeout(p_name: str):
        t0 = time.time()
        func = PROVIDER_FUNC_MAP.get(p_name)
        models = PROVIDER_MODELS.get(p_name, [])
        model = models[0] if models else None
        try:
            resp = await func(full_prompt, max_tokens, temp, model)
            elapsed = time.time() - t0
            if resp and len(resp.strip()) > 10:
                if state.redis_client:
                    await state.redis_client.lpush(f"metrics:{p_name}:latencies", elapsed)
                    await state.redis_client.ltrim(f"metrics:{p_name}:latencies", 0, 99)
                    await state.redis_client.incr(f"metrics:{p_name}:successes")
                    latencies = await state.redis_client.lrange(f"metrics:{p_name}:latencies", 0, 99)
                    if latencies:
                        avg_ttft = sum(float(l) for l in latencies) / len(latencies)
                        await state.redis_client.set(f"metrics:{p_name}:avg_ttft", avg_ttft)
                await record_provider_result(p_name, elapsed, success=True)
                if state.circuit_breaker is not None:
                    await state.circuit_breaker.record_success(p_name)
                return {"text": resp, "provider": p_name, "model": model, "latency": elapsed}
            raise ValueError("Empty output from provider")
        except Exception:
            elapsed = time.time() - t0
            if state.redis_client:
                await state.redis_client.incr(f"metrics:{p_name}:failures")
            await record_provider_result(p_name, elapsed, success=False)
            if state.circuit_breaker is not None:
                await state.circuit_breaker.record_failure(p_name)
            raise

    # ── FIX B4 ────────────────────────────────────────────────────────
    # Race the primary provider against a short TTFT clock. If it does
    # not answer within PIPELINE_PRIMARY_TTFT_MS (default 400 ms), fire
    # the fallbacks in parallel — do not wait for primary to finish.
    primary_task = asyncio.create_task(call_provider_with_timeout(primary_provider))
    primary_result = None
    primary_ttft_ms = int(os.getenv("PIPELINE_PRIMARY_TTFT_MS", "400"))
    try:
        done, _ = await asyncio.wait(
            [primary_task], timeout=primary_ttft_ms / 1000.0,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if done:
            for task in done:
                try:
                    primary_result = task.result()
                except Exception as e:
                    logger.debug("primary_provider_failed error=%s", e)
        else:
            logger.warning(
                "primary_ttft_exceeded provider=%s threshold_ms=%d",
                primary_provider, primary_ttft_ms,
            )
    except asyncio.CancelledError:
        if not primary_task.done():
            primary_task.cancel()
        await asyncio.gather(primary_task, return_exceptions=True)
        raise

    if primary_result is not None:
        final_text = sanitize_ai_output(primary_result["text"]) + \
            f"\n\n---\n*Generated through Axelr in {primary_result['latency']:.2f} seconds*"
        with contextlib.suppress(Exception):
            await _semantic_cache.set(contextualized_prompt, final_text)
        return {"success": True, "text": final_text,
                "provider": primary_result["provider"],
                "model_used": primary_result["model"],
                "tokens_used": len(final_text.split()),
                "latency_ms": round(primary_result["latency"] * 1000, 2)}

    # ── Parallel fallback wave ────────────────────────────────────────
    # All candidates race together. Whichever returns a valid response
    # first wins; the rest are cancelled.
    fallback_tasks: dict[asyncio.Task, str] = {}
    for p_name in fallback_providers:
        t = asyncio.create_task(
            call_provider_with_timeout(p_name),
            name=f"fallback::{p_name}",
        )
        fallback_tasks[t] = p_name

    is_pro_tier = tier in ("pro", "business")
    total_wall_clock_s = 30.0 if is_pro_tier else 15.0
    remaining_s = max(1.0, total_wall_clock_s - (time.time() - start))

    if fallback_tasks:
        try:
            for fut in asyncio.as_completed(
                fallback_tasks, timeout=remaining_s,
            ):
                try:
                    res = await fut
                except Exception:
                    continue
                # Got a winner — cancel the rest.
                for t in fallback_tasks:
                    if not t.done():
                        t.cancel()
                await asyncio.gather(*fallback_tasks, return_exceptions=True)
                if not primary_task.done():
                    primary_task.cancel()
                    await asyncio.gather(primary_task, return_exceptions=True)
                final_text = (
                    sanitize_ai_output(res["text"])
                    + f"\n\n---\n*Generated through Axelr in "
                      f"{res['latency']:.2f} seconds*"
                )
                with contextlib.suppress(Exception):
                    await _semantic_cache.set(contextualized_prompt, final_text)
                return {
                    "success": True, "text": final_text,
                    "provider": res["provider"], "model_used": res["model"],
                    "tokens_used": len(final_text.split()),
                    "latency_ms": round(res["latency"] * 1000, 2),
                }
        except asyncio.TimeoutError:
            pass
        finally:
            for t in fallback_tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*fallback_tasks, return_exceptions=True)

    if not primary_task.done():
        primary_task.cancel()
    await asyncio.gather(primary_task, return_exceptions=True)

    fallback = build_local_fallback_response(workspace, "core", prompt)
    return {"success": True, "text": fallback,
            "provider": "local", "model_used": "local-fallback",
            "tokens_used": len(fallback.split()),
            "latency_ms": round((time.time() - start) * 1000, 2)}


# ═══════════════════════════════════════════════════════════════════════════
# Streaming router
# ═══════════════════════════════════════════════════════════════════════════
async def stream_ai_response(
    workspace: str, task_type: str, prompt: str,
    history: list[dict] | None, files: list[dict] | None,
    max_tokens: int, temp: float, tier: str,
    user: dict | None = None, context: str = "",
):
    """Forward the resilient pipeline as Server-Sent Events."""
    from core.resilience import stream_pipeline

    started = time.time()
    fast_tier = (tier or "free") == "free" and user is None

    async for kind, payload in stream_pipeline(
        workspace=workspace, task_type=task_type, prompt=prompt,
        history=history, files=files,
        max_tokens=max_tokens, temp=temp, tier=tier,
        user=user, context=context, fast_tier=fast_tier,
    ):
        if kind == "thinking":
            # FIX B3 — pass the pipeline's thinking payload through
            # unmodified. The previous version emitted literal
            # `"Logo ... (animation) <stage: status>"` text to the client.
            yield f"event: thinking\ndata: {json.dumps(payload)}\n\n"
            continue
        if kind == "result":
            text = sanitize_ai_output(payload.get("text") or "")
            buf: list[str] = []
            tokens = text.split()
            for i, w in enumerate(tokens):
                buf.append(w)
                if len(buf) >= 8 or i == len(tokens) - 1:
                    yield f"data: {json.dumps({'text': ' '.join(buf) + ' '})}\n\n"
                    buf.clear()
                    await asyncio.sleep(0)
            elapsed = time.time() - started
            watermark = (f"\n\n---\n*{payload.get('provider','axelr')} · "
                         f"{payload.get('model_used','')} · {elapsed:.2f}s*")
            yield f"data: {json.dumps({'watermark': watermark})}\n\n"
            return

    yield f"data: {json.dumps({'error': 'AI pipeline produced no result'})}\n\n"


# ═══════════════════════════════════════════════════════════════════════════
# Sandbox
# ═══════════════════════════════════════════════════════════════════════════
async def _sandbox_execute(language: str, code: str, timeout: int = 8) -> dict[str, Any]:
    try:
        return await execute_code_on_worker(language, code, max(1, min(int(timeout or 8), 10)))
    except Exception as e:
        logger.warning("sandbox_execute_failed error=%s", e)
        return {"success": False, "output": "", "error": str(e)}


__all__ = [
    "generate_with_cascade",
    "route_ai_request_parallel", "route_ai_request_sequential",
    "stream_ai_response", "is_stub_response", "_sandbox_execute",
    "execute_axelr_hot_swap_engine", "get_hot_swap_engine",
    "get_twin_engine", "get_twin_engine_async",
    "_semantic_cache",
]