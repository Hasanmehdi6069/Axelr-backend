# core/resilience.py
"""
AXELR — Resilient Pipeline
============================
The entry point for every AI request. Orchestrates a deadline-aware
cascade through sanitize → cache → memory → providers → gateway → local
→ stub, and returns a single structured result.

Every returned result carries a `tier` field (from `core.failover.TIERS`)
so operators have ONE field to alert on. Never raises.

Cognition level (AXELR_COGNITION_LEVEL, env):
    0 = stub only    — remote off, cache off, twin off
    1 = fast         — cache + single provider, no gateway, no twin
    2 = standard     — full cascade, no twin (default)
    3 = elite        — full cascade + local twin

Deadlines (this revision):
    free / fast-flash tiers  →  15 s hard wall-clock ceiling
    pro / business tiers     →  30 s hard wall-clock ceiling

PII vault (Stage 3.2):
    On entry, the raw prompt is passed through `api.middleware.mask_pii`
    which swaps session-scoped sensitive values for `[REDACTED_n]`
    placeholders. The vault lives in `api.middleware._pii_sanitizer_vault`
    keyed on `{session_id}:{tenant_id}` and is popped on the way out via
    `restore_pii`. Redis and Mongo never see the mapping.

Stream-Heal buffer (Stage 3.3):
    When `request_id` is supplied, every text fragment that would be
    emitted to the client is mirrored into
    `api.middleware._stream_recovery_buffer[request_id]`. A middleware
    timeout (Stage 4.1) drains that buffer so the local twin can resume
    the stream without a client-visible reset.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable

from .failover import (                       # noqa: E402,F401
    HotSwapDetector, HotSwapResult, ProviderFunc,
)

logger = logging.getLogger("axelr.resilience")


# ═══════════════════════════════════════════════════════════════════════════
# Cognition level (Stage 4.2) — gates every downstream stage
# ═══════════════════════════════════════════════════════════════════════════
def _read_cognition_level() -> int:
    raw = (os.getenv("AXELR_COGNITION_LEVEL") or "2").strip()
    try:
        return max(0, min(3, int(raw)))
    except (TypeError, ValueError):
        logger.warning("cognition_level_invalid value=%r defaulting=2", raw)
        return 2


AXELR_COGNITION_LEVEL: int = _read_cognition_level()

_DEFAULT_DEADLINE_BY_LEVEL = {0: 2.0, 1: 6.0, 2: 10.0, 3: 15.0}
_DEFAULT_ROUNDS_BY_LEVEL   = {0: 1,   1: 2,   2: 4,    3: 6}
_DEFAULT_RACE_BY_LEVEL     = {0: 0,   1: 1,   2: 3,    3: 5}


# ── Tunables ──────────────────────────────────────────────────────────────
PIPELINE_DEADLINE_S        = float(os.getenv(
    "PIPELINE_DEADLINE_S",
    str(_DEFAULT_DEADLINE_BY_LEVEL[AXELR_COGNITION_LEVEL])))
PIPELINE_DEADLINE_FAST_S   = float(os.getenv("PIPELINE_DEADLINE_FAST_S", "6.0"))
CACHE_BUDGET_MS            = int(os.getenv("PIPELINE_CACHE_MS", "80"))
VECTORIZE_BUDGET_MS        = int(os.getenv("PIPELINE_VECTORIZE_MS", "200"))
MEMORY_BUDGET_MS           = int(os.getenv("PIPELINE_MEMORY_MS", "300"))
RACE_BUDGET_MS             = int(os.getenv("PIPELINE_RACE_MS", "600"))
RACE_WIDTH                 = int(os.getenv(
    "PIPELINE_RACE_WIDTH",
    str(_DEFAULT_RACE_BY_LEVEL[AXELR_COGNITION_LEVEL])))
SEQ_BUDGET_MS              = int(os.getenv("PIPELINE_SEQ_MS", "1800"))
GATEWAY_BUDGET_MS          = int(os.getenv("PIPELINE_GATEWAY_MS", "2500"))
HOT_SWAP_BUDGET_MS         = int(os.getenv("PIPELINE_HOT_SWAP_MS", "3000"))
PROVIDER_ATTEMPT_TIMEOUT_S = float(os.getenv("PIPELINE_PROVIDER_TIMEOUT_S", "8.0"))
MAX_ROUNDS                 = int(os.getenv(
    "PIPELINE_MAX_ROUNDS",
    str(_DEFAULT_ROUNDS_BY_LEVEL[AXELR_COGNITION_LEVEL])))

# ── Hard wall-clock ceilings by tier ──────────────────────────────────────
FAST_TIER_DEADLINE_S = float(os.getenv("PIPELINE_FAST_DEADLINE_S", "15.0"))
PRO_TIER_DEADLINE_S  = float(os.getenv("PIPELINE_PRO_DEADLINE_S", "30.0"))

_ms = lambda v: v / 1000.0


# ═══════════════════════════════════════════════════════════════════════════
# §1 — Degradation ladder
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True, slots=True)
class Tier:
    level: int
    name: str
    degraded: bool


TIERS: dict[str, Tier] = {
    "full":           Tier(0, "full", False),
    "reduced_ctx":    Tier(1, "reduced_ctx", True),
    "reduced_tokens": Tier(2, "reduced_tokens", True),
    "cached_similar": Tier(3, "cached_similar", True),
    "local":          Tier(4, "local", True),
    "stub":           Tier(5, "stub", True),
}


async def ladder_run(
    *,
    full: Callable[[], Awaitable[dict[str, Any]]],
    reduced_ctx: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    reduced_tokens: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    cached_similar: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    local: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    stub: Callable[[], Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    steps = [(n, f) for n, f in (
        ("full", full), ("reduced_ctx", reduced_ctx),
        ("reduced_tokens", reduced_tokens), ("cached_similar", cached_similar),
        ("local", local),
    ) if f is not None]

    for name, fn in steps:
        tier = TIERS[name]
        t0 = time.monotonic()
        try:
            result = await fn()
        except Exception as e:
            logger.warning("ladder_tier_failed tier=%s error=%s", name, e)
            continue
        if isinstance(result, dict) and result.get("success") and result.get("text"):
            result["tier"] = tier.name
            result["degraded"] = tier.degraded
            result["ladder_ms"] = round((time.monotonic() - t0) * 1000, 2)
            return result

    if stub is not None:
        try:
            result = await stub()
            if isinstance(result, dict):
                result.setdefault("success", True)
                result.setdefault("text", "⚠️ All AI services are temporarily busy.")
                result["tier"] = TIERS["stub"].name
                result["degraded"] = True
                return result
        except Exception as e:
            logger.warning("ladder_stub_failed error=%s", e)

    return {
        "success": False, "text": "⚠️ All AI services are temporarily busy.",
        "provider": "none", "model_used": "none",
        "tokens_used": 0, "latency_ms": 0.0,
        "tier": TIERS["stub"].name, "degraded": True,
    }


# ═══════════════════════════════════════════════════════════════════════════
# §2 — Helpers
# ═══════════════════════════════════════════════════════════════════════════
def _is_stub(text: str) -> bool:
    if not text:
        return True
    if text.lstrip().startswith("[stub]"):
        return True
    try:
        j = json.loads(text)
    except Exception:
        return False
    return isinstance(j, dict) and bool(j.get("_stub"))


def _pack(text: str, provider: str, model: str, latency_ms: float,
          *, tier: str = "full", extra: dict | None = None) -> dict[str, Any]:
    out = {
        "success": True, "text": text, "provider": provider,
        "model_used": model, "tokens_used": len(text.split()),
        "latency_ms": round(latency_ms, 2),
        "tier": tier, "degraded": tier != "full",
    }
    if extra:
        out.update(extra)
    return out


def _cache_key(workspace: str, task_type: str, prompt: str,
               context: str, history_key: str) -> str:
    ctx_hash = hashlib.sha256((context or "").encode()).hexdigest()[:16]
    norm = " ".join((prompt or "").lower().split())
    return hashlib.sha256(
        f"{workspace}:{task_type}:{norm}:{history_key}:{ctx_hash}".encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class GateDecision:
    force_retry: bool
    emit: bool
    buffered: str | None


class StreamQualityGate:
    def __init__(self, max_hold_chars: int = 80) -> None:
        self.max_hold_chars = max_hold_chars
        self._buffer: list[str] = []
        self._refusal_patterns = frozenset({
            "I'm sorry", "I cannot", "I can't", "I apologize",
            "unable to help", "not appropriate", "against policy"
        })

    def push(self, chunk: str) -> GateDecision:
        self._buffer.append(chunk)
        current = "".join(self._buffer)
        for pattern in self._refusal_patterns:
            if pattern.lower() in current.lower():
                return GateDecision(force_retry=True, emit=False, buffered=None)
        if len(current) >= self.max_hold_chars:
            self._buffer.clear()
            return GateDecision(force_retry=False, emit=True, buffered=current)
        return GateDecision(force_retry=False, emit=False, buffered=None)

    def flush(self) -> str:
        remaining = "".join(self._buffer)
        self._buffer.clear()
        return remaining


class TokenGovernor:
    def __init__(self) -> None:
        self._limits = {"free": 100000, "pro": 500000, "business": 2000000}

    def estimate_tokens(self, prompt: str) -> int:
        return int(len(prompt.split()) * 1.3)

    async def reserve(self, user_id: str, tier: str,
                      estimated_tokens: int) -> tuple[bool, int, float | None]:
        limit = self._limits.get(tier, self._limits["free"])
        remaining = limit - estimated_tokens
        return (remaining >= 0, max(0, remaining), None)


def add_watermark(text: str, provider: str, model: str, latency_ms: float) -> str:
    return text + f"\n\n---\n*{provider} · {model} · {round(latency_ms, 1)}ms*"


# ── PII Sanitization patterns ────────────────────────────────────────────
_PII_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', re.IGNORECASE), "email"),
    (re.compile(r'\b(\+\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b'), "phone"),
    (re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b'), "ip_address"),
    (re.compile(r'\b\d{3}-\d{2}-\d{4}\b'), "ssn"),
    (re.compile(r'\b(?:\d[ -]*?){13,16}\b'), "credit_card"),
    (re.compile(r'\b[A-Za-z0-9/+]{32,}\b'), "api_key"),
    (re.compile(r'sk-[A-Za-z0-9]{32,}\b'), "api_key"),
    (re.compile(r'pk_[A-Za-z0-9]{32,}\b'), "api_key"),
    (re.compile(r'\b(?:password|passwd|pwd|secret|token|auth)[^\s]*\s*[=:]\s*.{8,}\b', re.IGNORECASE), "credential"),
    (re.compile(r'\b[A-Z][a-z]+\s+[A-Z][a-z]+\b'), "full_name"),
    (re.compile(r'\b(?:bank|account|routing|swift|iban|bin|credit|debit|loan|mortgage)[^\s]*\s*#?\s*\d{4,}\b', re.IGNORECASE), "financial"),
    (re.compile(r'\b(?:usd|eur|gbp|cad|aud|jpy|cny)\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})?\b', re.IGNORECASE), "financial"),
    (re.compile(r'\$\d{1,3}(?:,\d{3})*(?:\.\d{2})?\b'), "financial"),
]

_PII_REPLACEMENTS = {
    "email": "[REDACTED_EMAIL]", "phone": "[REDACTED_PHONE]",
    "ip_address": "[REDACTED_IP]", "ssn": "[REDACTED_SSN]",
    "credit_card": "[REDACTED_CC]", "api_key": "[REDACTED_API_KEY]",
    "credential": "[REDACTED_CREDENTIAL]", "full_name": "[REDACTED_NAME]",
    "financial": "[REDACTED_FINANCIAL]",
}


def sanitize_pii(text: str) -> tuple[str, list[str]]:
    found_pii: set[str] = set()
    sanitized = text
    for pattern, pii_type in _PII_PATTERNS:
        if pattern.search(sanitized):
            sanitized = pattern.sub(_PII_REPLACEMENTS[pii_type], sanitized)
            found_pii.add(pii_type)
    return (sanitized, sorted(found_pii))


# ═══════════════════════════════════════════════════════════════════════════
# §3 — Resilient pipeline
# ═══════════════════════════════════════════════════════════════════════════
class ResilientPipeline:
    """Stateless per-call orchestrator. `run()` never raises."""

    def __init__(self) -> None:
        self.stats: dict[str, int] = {
            "runs": 0, "cache_hits": 0, "memory_hits": 0,
            "provider_wins": 0, "gateway_wins": 0, "hot_swap_wins": 0,
            "stub_fallbacks": 0, "rounds": 0,
        }
        self._token_governor = TokenGovernor()
        self._stream_gate = StreamQualityGate()

    # ─────────────────────────────────────────────────────────────────────
    # Session-scoped PII vault + Stream-Heal buffer helpers (Stage 3.2/3.3)
    # ─────────────────────────────────────────────────────────────────────
    @staticmethod
    def _mask_pii(session_key: str, tenant_id: str, text: str) -> str:
        """Session-scoped PII masking. No-op when sensitive_values is []."""
        try:
            from api.middleware import mask_pii
            return mask_pii(session_key, tenant_id, text, [])
        except Exception as e:
            logger.debug("mask_pii_unavailable error=%s", e)
            return text

    @staticmethod
    def _restore_pii(session_key: str, tenant_id: str, text: str) -> str:
        """Restore vault entries; drop the session's vault after use."""
        try:
            from api.middleware import restore_pii
            return restore_pii(session_key, tenant_id, text)
        except Exception as e:
            logger.debug("restore_pii_unavailable error=%s", e)
            return text

    # ─────────────────────────────────────────────────────────────────────
    # stream — full cascade
    # ─────────────────────────────────────────────────────────────────────
    async def stream(
        self, *, workspace: str, task_type: str, prompt: str,
        history: list[dict] | None, files: list[dict] | None,
        max_tokens: int, temp: float, tier: str, user: dict | None,
        context: str = "", fast_tier: bool = False,
        user_id: str | None = None,
        session_id: str | None = None,
        tenant_id: str | None = "default",
        request_id: str | None = None,
        want_elite: bool = False,
    ) -> AsyncIterator[tuple[str, Any]]:
        self.stats["runs"] += 1
        started = time.monotonic()

        # ── FIX B2 ─────────────────────────────────────────────────────
        # Per-request tier deadline. Local variable — never `self.*`.
        # The previous `self.pro_tier = True/False` mutated shared
        # singleton state and raced under concurrent requests.
        #
        # New ceilings per product spec:
        #   free / fast-flash  →  15 s
        #   pro / business     →  30 s
        is_pro_tier = tier in ("pro", "business")
        deadline_seconds = PRO_TIER_DEADLINE_S if is_pro_tier else FAST_TIER_DEADLINE_S
        deadline = started + deadline_seconds

        # ── Identity resolution ────────────────────────────────────────
        if user_id is None and user and isinstance(user, dict):
            uid = user.get("_id")
            user_id = str(uid) if uid else None
        if user and isinstance(user, dict):
            tenant_id = user.get("tenantId") or tenant_id or "default"
        tenant_id = tenant_id or "default"
        session_key = f"{session_id or 'anon'}:{tenant_id}"

        # ── Stream-Heal buffer binding (Stage 3.3) ─────────────────────
        stream_buffer: dict[str, str] | None = None
        if request_id:
            try:
                from api.middleware import _stream_recovery_buffer
                stream_buffer = _stream_recovery_buffer
            except Exception:
                stream_buffer = None

        def _capture_heal_text(text_chunk: str | None) -> None:
            """Mirror every emitted fragment into the recovery buffer."""
            if stream_buffer is None or not request_id or not text_chunk:
                return
            stream_buffer[request_id] = stream_buffer.get(request_id, "") + text_chunk

        def _restore_and_yield(payload: dict[str, Any]) -> dict[str, Any]:
            """Restore PII in a result payload and mirror its text."""
            if isinstance(payload.get("text"), str):
                payload["text"] = self._restore_pii(
                    session_key, tenant_id, payload["text"])
                _capture_heal_text(payload["text"])
            return payload

        # ── Stage 0: sanitize + PII mask ───────────────────────────────
        yield ("thinking", {"stage": "sanitize", "status": "running"})
        captured_tokens = ""
        if session_id and user_id:
            try:
                from api.middleware import get_stream_recovery_buffer
                recov = get_stream_recovery_buffer()
                resume_key = f"{session_id}:{user_id}"
                if resume_key in recov:
                    captured_tokens = recov.pop(resume_key, "")
                    logger.info("stream_recovery_activated request_id=%s captured_len=%d",
                                resume_key, len(captured_tokens))
                    if captured_tokens:
                        context = (context or "") + "\n" + captured_tokens
            except Exception as e:
                logger.debug("stream_recovery_unavailable error=%s", e)

        try:
            try:
                from api.prompts import (
                    contains_explicit, detect_manipulation, sanitize_input,
                )
            except Exception:
                from ..api.prompts import (  # type: ignore
                    contains_explicit, detect_manipulation, sanitize_input,
                )
            prompt = sanitize_input(prompt)
            prompt = self._mask_pii(session_key, tenant_id, prompt)
            prompt, pii_found = sanitize_pii(prompt)
            if pii_found:
                logger.info("pii_sanitized_in_prompt types=%s", ",".join(pii_found))
            if detect_manipulation(prompt) or contains_explicit(prompt):
                payload = {
                    "success": False, "text": "⚠️ Security violation.",
                    "provider": "security", "model_used": "filter",
                    "tokens_used": 0, "latency_ms": 0,
                    "tier": "full", "degraded": False,
                }
                yield ("result", _restore_and_yield(payload))
                return
        except Exception as e:
            logger.warning("pipeline_sanitize_failed error=%s", e)
        yield ("thinking", {"stage": "sanitize", "status": "ok"})

        # ── Stage 0a: Token governance (L1+) ────────────────────────────
        if AXELR_COGNITION_LEVEL >= 1 and user_id and tier:
            estimated_tokens = self._token_governor.estimate_tokens(prompt + (context or ""))
            allowed, remaining, retry_after = await self._token_governor.reserve(
                user_id, tier, estimated_tokens + max_tokens
            )
            if not allowed:
                payload = {
                    "success": False,
                    "text": f"⚠️ Token limit exceeded. {remaining} remaining, need {estimated_tokens + max_tokens}.",
                    "provider": "token_governor", "model_used": "governor",
                    "tokens_used": 0, "latency_ms": (time.monotonic() - started)*1000,
                    "retry_after": retry_after,
                    "tier": tier, "degraded": True,
                }
                yield ("result", _restore_and_yield(payload))
                return
            yield ("thinking", {"stage": "token_governor", "status": "ok", "remaining_tokens": remaining})

        # ── Stage 1: memory recall (L1+) ────────────────────────────────
        # FIX B2 (indentation) — the previous block was over-indented and
        # the `yield` was inside the `if get_memory is not None` guard.
        memory_block = ""
        if AXELR_COGNITION_LEVEL >= 1 and user_id:
            yield ("thinking", {"stage": "memory", "status": "running"})
            memory_success = False
            try:
                from ..core.memory import get_memory  # type: ignore
            except Exception:
                try:
                    from core.memory import get_memory  # type: ignore
                except Exception:
                    get_memory = None  # type: ignore
            if get_memory is not None:
                try:
                    mem = await get_memory()
                    hits = await asyncio.wait_for(
                        mem.recall(user_id=user_id, query=prompt, top_k=6),
                        timeout=_ms(MEMORY_BUDGET_MS),
                    )
                    if hits:
                        memory_block = "\n".join(h.text for h in hits[:6])
                        self.stats["memory_hits"] += 1
                        memory_success = True
                except asyncio.TimeoutError:
                    logger.warning("pipeline_memory_timeout - continuing pipeline")
                except Exception as e:
                    logger.warning("pipeline_memory_error - continuing pipeline error=%s",
                                   str(e)[:100])
            # Memory is optional — always continue
            yield ("thinking", {
                "stage": "memory",
                "status": "ok" if memory_success else "skipped",
            })

        if memory_block:
            context = (context + "\n\nRelevant history:\n" + memory_block).strip()

        hist_key = "|".join(
            f"{m.get('role', 'u')}:{str(m.get('text', ''))[:64]}"
            for m in (history or [])[-2:] if isinstance(m, dict)
        ) if history else ""
        cache_prompt = f"{hist_key}\nCurrent request: {prompt}" if hist_key else prompt

        # ── Main loop ─────────────────────────────────────────────────
        tried_providers: set[str] = set()
        for round_num in range(1, MAX_ROUNDS + 1):
            if time.monotonic() >= deadline:
                break
            self.stats["rounds"] += 1
            relaxed = round_num >= 2

            # Stage 2: semantic cache (L1+)
            if AXELR_COGNITION_LEVEL >= 1:
                cache_success = False
                try:
                    cache_res = await self._try_cache(cache_prompt, round_num)
                    if cache_res is not None:
                        yield ("thinking", {"stage": "cache", "status": "hit", "round": round_num})
                        sanitized_cache, pii_found = sanitize_pii(cache_res)
                        if pii_found:
                            logger.info("pii_sanitized_in_response types=%s", pii_found)
                        latency = (time.monotonic() - started) * 1000.0
                        final_text = add_watermark(sanitized_cache, "semantic_cache", "cache", latency)
                        payload = _pack(
                            final_text, "semantic_cache", "cache", latency,
                            tier="cached_similar",
                            extra={"cached": True, "round": round_num, "pii_found": pii_found},
                        )
                        yield ("result", _restore_and_yield(payload))
                        return
                    cache_success = True
                except Exception as e:
                    logger.warning("pipeline_cache_error - continuing pipeline error=%s",
                                   str(e)[:100])
                yield ("thinking", {"stage": "cache", "status": "miss" if cache_success else "skipped", "round": round_num})

            # Stage 3: wide-vectorize (L2+)
            if AXELR_COGNITION_LEVEL >= 2 and round_num >= 2:
                cache_res2 = await self._try_cache(cache_prompt, round_num, force_reembed=True)
                if cache_res2 is not None:
                    yield ("thinking", {"stage": "vectorize", "status": "hit", "round": round_num})
                    sanitized_cache2, pii_found = sanitize_pii(cache_res2)
                    if pii_found:
                        logger.info("pii_sanitized_in_response types=%s", pii_found)
                    latency = (time.monotonic() - started) * 1000.0
                    final_text = add_watermark(
                        sanitized_cache2, "semantic_cache", "vector-match", latency)
                    payload = _pack(
                        final_text, "semantic_cache", "vector-match", latency,
                        tier="cached_similar",
                        extra={"cached": True, "round": round_num,
                               "pii_found": pii_found},
                    )
                    yield ("result", _restore_and_yield(payload))
                    return
                yield ("thinking", {"stage": "vectorize", "status": "miss", "round": round_num})

            # Stage 4: remote providers (L1+)
            if AXELR_COGNITION_LEVEL >= 1 and deadline - time.monotonic() > 0.4:
                prov_res = await self._try_providers(
                    workspace=workspace, task_type=task_type, prompt=prompt,
                    history=history, context=context,
                    max_tokens=max_tokens, temp=temp, tier=tier, user=user,
                    deadline=deadline, skip=tried_providers,
                    relaxed=relaxed, round_num=round_num,
                )
                if prov_res is not None:
                    yield ("thinking", {
                        "stage": "providers", "status": "ok",
                        "winner": prov_res["provider"],
                        "model": prov_res["model_used"], "round": round_num,
                    })
                    sanitized_text, pii_found = sanitize_pii(prov_res["text"])
                    if pii_found:
                        logger.info("pii_sanitized_in_response types=%s", pii_found)
                    latency = (time.monotonic() - started) * 1000.0
                    prov_res["latency_ms"] = round(latency, 2)
                    prov_res["text"] = add_watermark(
                        sanitized_text, prov_res["provider"],
                        prov_res["model_used"], latency)
                    prov_res["pii_found"] = pii_found
                    yield ("result", _restore_and_yield(prov_res))
                    return
                yield ("thinking", {"stage": "providers", "status": "all_failed",
                                    "round": round_num})

            # Stage 5: LLM gateway (L2+)
            if AXELR_COGNITION_LEVEL >= 2 and deadline - time.monotonic() > 0.5:
                gw_res = await self._try_gateway(
                    prompt=prompt, context=context, history=history,
                    max_tokens=max_tokens, temp=temp,
                    deadline=deadline, round_num=round_num,
                )
                if gw_res is not None:
                    yield ("thinking", {"stage": "gateway", "status": "ok", "round": round_num})
                    latency = (time.monotonic() - started) * 1000.0
                    gw_res["latency_ms"] = round(latency, 2)
                    sanitized_text, pii_found = sanitize_pii(gw_res["text"])
                    if pii_found:
                        logger.info("pii_sanitized_in_response types=%s", pii_found)
                    gw_res["text"] = add_watermark(
                        sanitized_text, gw_res["provider"],
                        gw_res["model_used"], latency
                    )
                    gw_res["pii_found"] = pii_found
                    yield ("result", _restore_and_yield(gw_res))
                    return

            # Stage 6: local twin (L3 only) — Stream-Heal delivery
            if AXELR_COGNITION_LEVEL >= 3 and deadline - time.monotonic() > 0.4:
                hs_res = await self._try_local(
                    workspace, task_type, prompt, deadline, round_num,
                    context=context, tier=tier,
                )
                if hs_res is not None:
                    yield ("thinking", {"stage": "local", "status": "ok", "round": round_num})
                    latency = (time.monotonic() - started) * 1000.0
                    hs_res["latency_ms"] = round(latency, 2)
                    sanitized_text, pii_found = sanitize_pii(hs_res["text"])
                    if pii_found:
                        logger.info("pii_sanitized_in_response types=%s", pii_found)
                    hs_res["text"] = add_watermark(
                        sanitized_text, hs_res["provider"],
                        hs_res["model_used"], latency
                    )
                    hs_res["pii_found"] = pii_found
                    yield ("result", _restore_and_yield(hs_res))
                    return
                yield ("thinking", {"stage": "local", "status": "fail", "round": round_num})

            # Do NOT clear tried_providers between rounds — keep the full
            # failure history so no provider is retried forever.

        # ── Stage 7: emergency stub ───────────────────────────────────
        self.stats["stub_fallbacks"] += 1
        elapsed = time.monotonic() - started
        logger.critical(
            "pipeline_exhausted_all_attempts user=%s elapsed=%.1fs tier=%s",
            user_id, elapsed, tier,
        )

        # Build a local fallback string first (may be empty)
        try:
            from ..api.providers import build_local_fallback_response  # type: ignore
        except Exception:
            try:
                from api.providers import build_local_fallback_response  # type: ignore
            except Exception:
                def build_local_fallback_response(w, t, p):
                    return ""

        base_text = build_local_fallback_response(workspace, task_type, prompt)

        # If we have burned the tier's entire wall-clock ceiling, route the
        # user to the multi-agent system. Otherwise emit a "busy" stub.
        if elapsed >= deadline_seconds:
            text = (base_text if base_text and len(base_text.strip()) > 10
                    else "⚠️ High traffic detected. Routing you to our "
                         "multi-agent system. Please wait a moment.")
            provider_label = "multi-agent-router"
            model_label = "orchestrator"
            extra = {"routing_to_multi_agent": True}
        else:
            text = (base_text if base_text and len(base_text.strip()) > 10
                    else "⚠️ All AI services are temporarily busy.")
            provider_label = "local-fallback"
            model_label = "local"
            extra = {"last_ditch": True}

        latency = (time.monotonic() - started) * 1000.0
        sanitized_text, pii_found = sanitize_pii(text)
        final_text = add_watermark(
            sanitized_text, provider_label, model_label, latency)
        stub_result = _pack(
            final_text, provider_label, model_label, latency,
            tier="stub", extra={**extra, "pii_found": pii_found},
        )
        yield ("thinking", {"stage": "fallback", "status": "emergency"})
        yield ("result", _restore_and_yield(stub_result))

    async def run(self, **kwargs) -> dict[str, Any]:
        last: dict[str, Any] = {
            "success": False, "text": "", "provider": "none",
            "model_used": "none", "tokens_used": 0, "latency_ms": 0,
            "tier": "stub", "degraded": True,
        }
        async for kind, payload in self.stream(**kwargs):
            if kind == "result":
                last = payload
        return last

    async def generate(self, **kwargs) -> dict[str, Any]:
        return await self.run(**kwargs)

    # ── stage impls ───────────────────────────────────────────────────
    async def _try_cache(self, prompt: str, round_num: int,
                         *, force_reembed: bool = False) -> str | None:
        try:
            from ..api.state import state  # type: ignore
        except Exception:
            try:
                from api.state import state  # type: ignore
            except Exception:
                return None
        cache = getattr(state, "semantic_cache", None)
        if cache is None:
            return None
        budget = _ms(VECTORIZE_BUDGET_MS if force_reembed else CACHE_BUDGET_MS)
        try:
            hit = await asyncio.wait_for(cache.get(prompt), timeout=budget)
            if hit:
                self.stats["cache_hits"] += 1
            return hit
        except asyncio.TimeoutError:
            logger.debug("pipeline_cache_timeout round=%d", round_num)
            return None
        except Exception as e:
            logger.debug("pipeline_cache_error round=%d error=%s", round_num, e)
            return None

    async def _try_providers(
        self, *, workspace: str, task_type: str, prompt: str,
        history: list[dict] | None, context: str,
        max_tokens: int, temp: float, tier: str, user: dict | None,
        deadline: float, skip: set[str], relaxed: bool, round_num: int,
    ) -> dict[str, Any] | None:
        try:
            from ..api.prompts import _get_system_prompt  # type: ignore
            from ..api.providers import (  # type: ignore
                PROVIDER_FUNC_MAP, PROVIDER_KEY_CHECK, PROVIDER_MODELS,
                get_dynamically_ranked_providers,
            )
            from ..api.state import state  # type: ignore
        except Exception:
            try:
                from api.prompts import _get_system_prompt  # type: ignore
                from api.providers import (  # type: ignore
                    PROVIDER_FUNC_MAP, PROVIDER_KEY_CHECK, PROVIDER_MODELS,
                    get_dynamically_ranked_providers,
                )
                from api.state import state  # type: ignore
            except Exception as e:
                logger.warning("pipeline_providers_import_failed error=%s", e)
                return None

        try:
            system_prompt = _get_system_prompt(workspace, task_type)
        except Exception:
            system_prompt = "You are Axelr, a helpful AI assistant."

        full_prompt = f"{system_prompt}\n\n"
        if context:
            full_prompt += f"Context: {context}\n\n"
        if history:
            recent = [f"{m.get('role', 'user')}: {m.get('text', '')}"
                      for m in history[-4:]
                      if isinstance(m, dict) and m.get("text")]
            if recent:
                full_prompt += "Previous conversation:\n" + "\n".join(recent) + "\n\n"
        full_prompt += f"User request: {prompt}"

        try:
            ranked = await get_dynamically_ranked_providers(workspace)
        except Exception:
            ranked = list(PROVIDER_FUNC_MAP.keys())

        def _eligible(p: str) -> bool:
            if p == "local" or p in skip:
                return False
            if not PROVIDER_KEY_CHECK.get(p, False):
                return False
            if not PROVIDER_MODELS.get(p):
                return False
            return True

        available = [p for p in ranked if _eligible(p)]
        race_budget = _ms(RACE_BUDGET_MS)

        while available and RACE_WIDTH > 0 and time.monotonic() < deadline:
            wave = available[:RACE_WIDTH]
            available = available[RACE_WIDTH:]
            if not relaxed:
                cb = getattr(state, "circuit_breaker", None)
                if cb is not None:
                    filtered = []
                    for p in wave:
                        try:
                            if await cb.is_available(p):
                                filtered.append(p)
                        except Exception:
                            filtered.append(p)
                    wave = filtered
            if not wave:
                continue

            result = await self._race(
                wave, full_prompt, max_tokens, temp,
                PROVIDER_FUNC_MAP, PROVIDER_MODELS, timeout_s=race_budget,
            )
            if result is not None:
                self.stats["provider_wins"] += 1
                try:
                    cb = getattr(state, "circuit_breaker", None)
                    if cb is not None:
                        await cb.record_success(result["provider"])
                except Exception:
                    pass
                skip.add(result["provider"])
                return result
            skip.update(wave)

        # Sequential walk
        seq_deadline = min(deadline, time.monotonic() + _ms(SEQ_BUDGET_MS))
        for p in list(available):
            if time.monotonic() >= seq_deadline:
                break
            if not relaxed:
                cb = getattr(state, "circuit_breaker", None)
                if cb is not None:
                    try:
                        if not await cb.is_available(p):
                            continue
                    except Exception:
                        pass
            model = (PROVIDER_MODELS.get(p) or [None])[0]
            func = PROVIDER_FUNC_MAP.get(p)
            if not func or not model:
                continue
            for attempt in range(2):
                if time.monotonic() >= seq_deadline:
                    break
                try:
                    t0 = time.monotonic()
                    resp = await asyncio.wait_for(
                        func(full_prompt, max_tokens, temp, model),
                        timeout=min(PROVIDER_ATTEMPT_TIMEOUT_S,
                                    max(0.5, seq_deadline - time.monotonic())),
                    )
                    if resp and len(resp.strip()) > 3:
                        self.stats["provider_wins"] += 1
                        skip.add(p)
                        return _pack(resp, p, model, (time.monotonic() - t0) * 1000.0)
                except Exception as e:
                    logger.debug("pipeline_provider_fail provider=%s attempt=%d error=%s",
                                 p, attempt + 1, e)
                    await asyncio.sleep(0.15 * (attempt + 1))
            skip.add(p)
        return None

    async def _race(self, wave: list[str], full_prompt: str,
                    max_tokens: int, temp: float, func_map: dict,
                    models_map: dict, *, timeout_s: float) -> dict[str, Any] | None:
        tasks: list[asyncio.Task] = []
        for p in wave:
            func = func_map.get(p)
            models = models_map.get(p) or []
            if not func or not models:
                continue
            tasks.append(asyncio.create_task(
                self._one_provider(p, models[0], func, full_prompt, max_tokens, temp),
                name=f"race::{p}",
            ))
        if not tasks:
            return None
        try:
            for fut in asyncio.as_completed(tasks, timeout=timeout_s):
                try:
                    res = await fut
                except Exception:
                    continue
                if res is not None:
                    for t in tasks:
                        if not t.done():
                            t.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    return res
        except asyncio.TimeoutError:
            pass
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return None

    @staticmethod
    async def _one_provider(name: str, model: str, func: Callable, prompt: str,
                            max_tokens: int, temp: float) -> dict[str, Any] | None:
        t0 = time.monotonic()
        try:
            resp = await func(prompt, max_tokens, temp, model)
            if resp and len(resp.strip()) > 3:
                return _pack(resp, name, model, (time.monotonic() - t0) * 1000.0)
        except Exception:
            return None
        return None

    async def _try_gateway(self, *, prompt: str, context: str,
                           history: list[dict] | None, max_tokens: int,
                           temp: float, deadline: float,
                           round_num: int) -> dict[str, Any] | None:
        try:
            from ..api.state import state  # type: ignore
        except Exception:
            try:
                from api.state import state  # type: ignore
            except Exception:
                return None
        url = getattr(state, "gateway_url", None)
        key = getattr(state, "gateway_key", None)
        client = getattr(state, "gateway_http", None)
        if not url or client is None:
            return None
        budget = min(_ms(GATEWAY_BUDGET_MS), max(0.5, deadline - time.monotonic()))
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        body = {"model": "auto", "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens, "temperature": temp, "stream": False}
        t0 = time.monotonic()
        try:
            r = await asyncio.wait_for(
                client.post(f"{url.rstrip('/')}/v1/chat/completions",
                            headers=headers, json=body),
                timeout=budget,
            )
            r.raise_for_status()
            data = r.json()
            text = (data.get("choices", [{}])[0]
                    .get("message", {}).get("content") or "").strip()
            if not text or _is_stub(text):
                return None
            self.stats["gateway_wins"] += 1
            return _pack(text, "llm_gateway", "gateway",
                         (time.monotonic() - t0) * 1000.0)
        except (asyncio.TimeoutError, Exception) as e:
            logger.debug("pipeline_gateway_fail round=%d error=%s", round_num, e)
            return None

    async def _try_local(self, workspace: str, task_type: str, prompt: str,
                         deadline: float, round_num: int,
                         *, context: str = "",
                         tier: str = "free") -> dict[str, Any] | None:
        """
        Local twin fallback. ``context`` carries the Stream-Heal buffer
        (Stage 3.3) so a timed-out remote stream resumes locally with the
        tokens already emitted — no client-visible reset.
        ``tier`` is forwarded so the twin engine can enforce its own
        tight budget (free ≤ 15 s total, pro ≤ 30 s total).
        """
        try:
            from .twin_engine import execute_local_inference
        except Exception:
            return None
        budget = min(_ms(HOT_SWAP_BUDGET_MS), max(0.3, deadline - time.monotonic()))
        full_prompt = f"{context}\n\n{prompt}" if context else prompt
        try:
            text = await asyncio.wait_for(
                execute_local_inference(workspace, task_type, full_prompt, tier=tier),
                timeout=budget,
            )
        except asyncio.TimeoutError:
            logger.warning("pipeline_local_timeout round=%d", round_num)
            return None
        except Exception as e:
            logger.warning("pipeline_local_error round=%d error=%s", round_num, e)
            return None
        if not text or _is_stub(text):
            return None
        self.stats["hot_swap_wins"] += 1
        return _pack(text, "local_twin", "twin", budget * 1000.0, tier="local")


# ═══════════════════════════════════════════════════════════════════════════
# §4 — Singletons
# ═══════════════════════════════════════════════════════════════════════════
_PIPELINE = ResilientPipeline()
_DETECTOR: HotSwapDetector | None = None


async def run_pipeline(**kwargs) -> dict[str, Any]:
    return await _PIPELINE.run(**kwargs)


async def stream_pipeline(**kwargs) -> AsyncIterator[tuple[str, Any]]:
    async for item in _PIPELINE.stream(**kwargs):
        yield item


def get_pipeline() -> ResilientPipeline:
    return _PIPELINE


def get_hot_swap_detector(providers: dict[str, ProviderFunc] | None = None,
                          **kwargs: Any) -> HotSwapDetector | None:
    """Canonical factory — sole definition of this symbol in the module."""
    global _DETECTOR
    if _DETECTOR is not None:
        return _DETECTOR
    if providers is None:
        try:
            from api.providers import PROVIDER_FUNC_MAP  # type: ignore
            providers = {k: v for k, v in PROVIDER_FUNC_MAP.items() if k != "local"}
        except Exception as e:
            logger.warning("hot_swap_provider_map_unavailable error=%s", e)
            return None
    _DETECTOR = HotSwapDetector(providers, **kwargs)
    logger.info("hot_swap_detector_ready providers=%d", len(providers))
    return _DETECTOR


ProviderCascade = ResilientPipeline


def audit() -> dict[str, Any]:
    return {
        "pipeline_stats": dict(_PIPELINE.stats),
        "tiers": list(TIERS.keys()),
        "cognition_level": AXELR_COGNITION_LEVEL,
        "max_rounds": MAX_ROUNDS,
        "race_width": RACE_WIDTH,
        "deadline_s": PIPELINE_DEADLINE_S,
        "fast_deadline_s": FAST_TIER_DEADLINE_S,
        "pro_deadline_s": PRO_TIER_DEADLINE_S,
        "hot_swap": bool(_DETECTOR),
    }


__all__ = [
    "Tier", "TIERS", "ladder_run",
    "ResilientPipeline", "ProviderCascade", "get_pipeline",
    "run_pipeline", "stream_pipeline",
    "PIPELINE_DEADLINE_S", "PIPELINE_DEADLINE_FAST_S",
    "FAST_TIER_DEADLINE_S", "PRO_TIER_DEADLINE_S",
    "AXELR_COGNITION_LEVEL",
    "ProviderFunc", "HotSwapResult", "HotSwapDetector", "get_hot_swap_detector",
    "audit",
]