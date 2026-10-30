# core/pipeline.py
"""
AXELR — Resilient Pipeline (canonical algorithm)
=================================================
Single entry point for every AI request. Orchestrates a deadline-aware
cascade through sanitize → cache → memory → providers → gateway →
local → stub, and returns one structured result.

Every result carries a ``tier`` field so operators have ONE field to
alert on. Never raises.

Companion modules (do not duplicate here):
    * ``core.orchestration``  — pipeline singleton (``get_pipeline`` /
                                ``run_pipeline`` / ``stream_pipeline``)
                                + hot-swap detector + elite hooks
    * ``core.failover``       — ``Tier``, ``TIERS``, ``HotSwapDetector``,
                                ``ladder_run``, ``ProviderFunc``
    * ``core.infra``          — canonical ``CircuitBreaker``
    * ``core.adaptive_control`` — canonical ``StreamQualityGate``
                                  (not used by this module; see note below)

Cognition level (``AXELR_COGNITION_LEVEL``, env):
    0 = stub only    1 = fast (cache + single provider)
    2 = standard     3 = elite (full cascade + local twin)

Deadlines (hard wall-clock ceilings per tier):
    free / fast-flash → 15 s    pro / business → 30 s

Stages and their budgets (env-overridable, defaults shown):
    sanitize        — 0 ms (synchronous)
    token governor  — 0 ms (stubbed; see ``TokenGovernor``)
    memory recall   — 300 ms
    semantic cache  — 80 ms (vectorize re-check 200 ms)
    providers race  — 600 ms wave, 1800 ms sequential walk
    gateway         — 2500 ms
    local twin      — 3000 ms

PII history note
----------------
The canonical sanitizer lives in ``core.utils.security_and_streaming``
and detects email/phone/ip/ssn/credit-card/api-key/credential/financial.
Two patterns that historically existed only in the pipeline
(``full_name`` and ``bank/account``) are preserved here as
``_PIPELINE_EXTRA_PII``. Their canonical home should eventually be
``security_and_streaming._PII_PATTERNS``; until that move lands,
``sanitize_pii()`` in this module is the superset used at runtime.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, AsyncIterator, Awaitable, Callable

from .utils.security_and_streaming import sanitize as _canonical_sanitize

logger = logging.getLogger("axelr.pipeline")


# ═══════════════════════════════════════════════════════════════════════════
# §1 — Configuration (env-derived, module-level constants)
# ═══════════════════════════════════════════════════════════════════════════
def _read_cognition_level() -> int:
    """Parse ``AXELR_COGNITION_LEVEL``; clamp to [0, 3]; default 2."""
    raw = (os.getenv("AXELR_COGNITION_LEVEL") or "2").strip()
    try:
        return max(0, min(3, int(raw)))
    except (TypeError, ValueError):
        logger.warning("cognition_level_invalid value=%r defaulting=2", raw)
        return 2


AXELR_COGNITION_LEVEL: int = _read_cognition_level()

_DEADLINE_BY_LEVEL: dict[int, float] = {0: 2.0, 1: 6.0, 2: 10.0, 3: 15.0}
_ROUNDS_BY_LEVEL: dict[int, int] = {0: 1, 1: 2, 2: 4, 3: 6}
_RACE_BY_LEVEL: dict[int, int] = {0: 0, 1: 1, 2: 3, 3: 5}

PIPELINE_DEADLINE_S: float = float(os.getenv(
    "PIPELINE_DEADLINE_S", str(_DEADLINE_BY_LEVEL[AXELR_COGNITION_LEVEL])))
CACHE_BUDGET_MS: int = int(os.getenv("PIPELINE_CACHE_MS", "80"))
VECTORIZE_BUDGET_MS: int = int(os.getenv("PIPELINE_VECTORIZE_MS", "200"))
MEMORY_BUDGET_MS: int = int(os.getenv("PIPELINE_MEMORY_MS", "300"))
RACE_BUDGET_MS: int = int(os.getenv("PIPELINE_RACE_MS", "600"))
RACE_WIDTH: int = int(os.getenv(
    "PIPELINE_RACE_WIDTH", str(_RACE_BY_LEVEL[AXELR_COGNITION_LEVEL])))
SEQ_BUDGET_MS: int = int(os.getenv("PIPELINE_SEQ_MS", "1800"))
GATEWAY_BUDGET_MS: int = int(os.getenv("PIPELINE_GATEWAY_MS", "2500"))
HOT_SWAP_BUDGET_MS: int = int(os.getenv("PIPELINE_HOT_SWAP_MS", "3000"))
PROVIDER_ATTEMPT_TIMEOUT_S: float = float(os.getenv(
    "PIPELINE_PROVIDER_TIMEOUT_S", "8.0"))
MAX_ROUNDS: int = int(os.getenv(
    "PIPELINE_MAX_ROUNDS", str(_ROUNDS_BY_LEVEL[AXELR_COGNITION_LEVEL])))

FAST_TIER_DEADLINE_S: float = float(os.getenv(
    "PIPELINE_FAST_DEADLINE_S", "15.0"))
PRO_TIER_DEADLINE_S: float = float(os.getenv(
    "PIPELINE_PRO_DEADLINE_S", "30.0"))


def _ms(value_ms: float) -> float:
    """Milliseconds → seconds."""
    return value_ms / 1000.0


# ═══════════════════════════════════════════════════════════════════════════
# §2 — Small helpers
# ═══════════════════════════════════════════════════════════════════════════
def _is_stub(text: str) -> bool:
    """True for empty, ``[stub]``-prefixed, or ``{"_stub": true}`` text."""
    if not text:
        return True
    if text.lstrip().startswith("[stub]"):
        return True
    try:
        parsed = json.loads(text)
    except Exception:
        return False
    return isinstance(parsed, dict) and bool(parsed.get("_stub"))


def _pack(
    text: str, provider: str, model: str, latency_ms: float,
    *, tier: str = "full", extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical result dict. ``degraded`` is True iff
    ``tier != 'full'``. ``extra`` keys are merged last (may overwrite)."""
    out: dict[str, Any] = {
        "success": True, "text": text, "provider": provider,
        "model_used": model, "tokens_used": len(text.split()),
        "latency_ms": round(latency_ms, 2),
        "tier": tier, "degraded": tier != "full",
    }
    if extra:
        out.update(extra)
    return out


def add_watermark(
    text: str, provider: str, model: str, latency_ms: float,
) -> str:
    """Append the provenance footer used on every successful response."""
    return (
        text + f"\n\n---\n*{provider} · {model} · {round(latency_ms, 1)}ms*"
    )


# Two patterns that historically lived only in this module. Tracked for
# migration into ``security_and_streaming._PII_PATTERNS``.
_PIPELINE_EXTRA_PII: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(
        r"\b(?:bank|account|routing|swift|iban|bin|credit|debit|loan|mortgage)"
        r"[^\s]*\s*#?\s*\d{4,}\b", re.IGNORECASE),
     "financial", "[REDACTED_FINANCIAL]"),
    (re.compile(r"\b[A-Z][a-z]+\s+[A-Z][a-z]+\b"),
     "full_name", "[REDACTED_NAME]"),
)


def sanitize_pii(text: str) -> tuple[str, list[str]]:
    """Pipeline-level PII sanitizer.

    Delegates to ``security_and_streaming.sanitize`` (canonical), then
    applies ``_PIPELINE_EXTRA_PII`` so pipeline-specific patterns still
    fire. Result is a strict superset of the canonical detector's output.

    Returns ``(sanitized_text, sorted_list_of_types_found)``.
    Never raises.
    """
    sanitized, found = _canonical_sanitize(text)
    found_set = set(found)
    for pattern, pii_type, replacement in _PIPELINE_EXTRA_PII:
        if pattern.search(sanitized):
            sanitized = pattern.sub(replacement, sanitized)
            found_set.add(pii_type)
    return sanitized, sorted(found_set)


class TokenGovernor:
    """Per-tier token budget ledger.

    **Current state: stub.** ``reserve()`` returns the tier's full quota
    without persisting anything — it does NOT actually decrement. A
    future revision backed by Redis will swap the ``reserve`` body for
    real accounting; the contract stays identical.

    Contract:
        * ``estimate_tokens(prompt)`` → cheap ~1.3 * word count.
        * ``reserve(user_id, tier, estimated)`` → ``(allowed, remaining,
          retry_after)``. ``retry_after`` is always ``None`` in this stub.
        * Never raises.
    """
    _LIMITS: dict[str, int] = {
        "free": 100_000, "pro": 500_000, "business": 2_000_000,
    }

    @staticmethod
    def estimate_tokens(prompt: str) -> int:
        """Approximate token count (1.3 * word count)."""
        return int(len(prompt.split()) * 1.3)

    async def reserve(
        self, user_id: str, tier: str, estimated_tokens: int,
    ) -> tuple[bool, int, float | None]:
        """Stub reservation. Returns ``(allowed, remaining, retry_after)``."""
        limit = self._LIMITS.get(tier, self._LIMITS["free"])
        remaining = limit - estimated_tokens
        return (remaining >= 0, max(0, remaining), None)


# ═══════════════════════════════════════════════════════════════════════════
# §3 — ResilientPipeline
# ═══════════════════════════════════════════════════════════════════════════
class ResilientPipeline:
    """Stateless per-call orchestrator.

    Only ``self.stats`` (counter dict) is mutable instance state.
    Per-request tier selection is a **local variable** in ``stream()`` —
    never ``self.*`` — so concurrent requests cannot clobber one another.

    Contract:
        * ``stream(**kwargs)`` yields ``("thinking", dict)`` events plus
          exactly one terminal ``("result", dict)``. Never raises.
        * ``run(**kwargs)`` collapses ``stream`` to the result dict.
          Returns a stub-shaped result on total failure.
    """

    def __init__(self) -> None:
        self.stats: dict[str, int] = {
            "runs": 0, "cache_hits": 0, "memory_hits": 0,
            "provider_wins": 0, "gateway_wins": 0, "hot_swap_wins": 0,
            "stub_fallbacks": 0, "rounds": 0,
        }
        self._token_governor = TokenGovernor()

    # ── PII vault helpers ────────────────────────────────────────────
    @staticmethod
    def _mask_pii(session_key: str, tenant_id: str, text: str) -> str:
        """Session-scoped PII mask. No-op when middleware is absent."""
        try:
            from api.middleware import mask_pii  # type: ignore
            return mask_pii(session_key, tenant_id, text, [])
        except Exception as e:
            logger.debug("mask_pii_unavailable error=%s", e)
            return text

    @staticmethod
    def _restore_pii(session_key: str, tenant_id: str, text: str) -> str:
        """Restore vault entries and drop the session's vault."""
        try:
            from api.middleware import restore_pii  # type: ignore
            return restore_pii(session_key, tenant_id, text)
        except Exception as e:
            logger.debug("restore_pii_unavailable error=%s", e)
            return text

    # ── the cascade ──────────────────────────────────────────────────
    async def stream(
        self, *, workspace: str, task_type: str, prompt: str,
        history: list[dict[str, Any]] | None,
        files: list[dict[str, Any]] | None,
        max_tokens: int, temp: float, tier: str,
        user: dict[str, Any] | None,
        context: str = "",
        fast_tier: bool = False,      # RESERVED — accepted, unused
        user_id: str | None = None,
        session_id: str | None = None,
        tenant_id: str | None = "default",
        request_id: str | None = None,
        want_elite: bool = False,     # RESERVED — accepted, unused
    ) -> AsyncIterator[tuple[str, Any]]:
        """Run the full cascade. Never raises.

        ``fast_tier`` and ``want_elite`` are retained for backward
        compatibility with callers that predate per-request tier
        deadlines; they are accepted and ignored.
        """
        self.stats["runs"] += 1
        started = time.monotonic()

        is_pro = tier in ("pro", "business")
        deadline_s = PRO_TIER_DEADLINE_S if is_pro else FAST_TIER_DEADLINE_S
        deadline = started + deadline_s

        # Identity resolution
        if user_id is None and user and isinstance(user, dict):
            uid = user.get("_id")
            user_id = str(uid) if uid else None
        if user and isinstance(user, dict):
            tenant_id = user.get("tenantId") or tenant_id or "default"
        tenant_id = tenant_id or "default"
        session_key = f"{session_id or 'anon'}:{tenant_id}"

        # Stream-Heal buffer binding
        stream_buffer: dict[str, str] | None = None
        if request_id:
            try:
                from api.middleware import _stream_recovery_buffer  # type: ignore
                stream_buffer = _stream_recovery_buffer
            except Exception:
                stream_buffer = None

        def _capture(text_chunk: str | None) -> None:
            if stream_buffer is None or not request_id or not text_chunk:
                return
            stream_buffer[request_id] = (
                stream_buffer.get(request_id, "") + text_chunk)

        def _finalize(
            payload: dict[str, Any], *,
            extra: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            """Sanitize, watermark (success only), restore PII, mirror."""
            latency = (time.monotonic() - started) * 1000.0
            sanitized, found = sanitize_pii(payload.get("text", ""))
            if found:
                logger.info(
                    "pii_sanitized_in_response types=%s", ",".join(found))
            payload["latency_ms"] = round(latency, 2)
            if payload.get("success", True):
                payload["text"] = add_watermark(
                    sanitized, payload["provider"],
                    payload["model_used"], latency)
            else:
                payload["text"] = sanitized
            payload["pii_found"] = found
            if extra:
                payload.update(extra)
            payload["text"] = self._restore_pii(
                session_key, tenant_id, payload["text"])
            _capture(payload["text"])
            return payload

        # ── Stage 0: sanitize + PII mask ────────────────────────────
        yield ("thinking", {"stage": "sanitize", "status": "running"})
        try:
            try:
                from api.prompts import (  # type: ignore
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
                logger.info(
                    "pii_sanitized_in_prompt types=%s", ",".join(pii_found))
            if detect_manipulation(prompt) or contains_explicit(prompt):
                payload = _pack(
                    "⚠️ Security violation.", "security", "filter", 0.0)
                payload["success"] = False
                yield ("result", _finalize(payload))
                return
        except Exception as e:
            logger.warning("pipeline_sanitize_failed error=%s", e)
        yield ("thinking", {"stage": "sanitize", "status": "ok"})

        # ── Stage 0a: token governance (L1+) ────────────────────────
        if AXELR_COGNITION_LEVEL >= 1 and user_id and tier:
            estimated = self._token_governor.estimate_tokens(
                prompt + (context or ""))
            allowed, remaining, retry_after = (
                await self._token_governor.reserve(
                    user_id, tier, estimated + max_tokens))
            if not allowed:
                payload = _pack(
                    f"⚠️ Token limit exceeded. {remaining} remaining, "
                    f"need {estimated + max_tokens}.",
                    "token_governor", "governor", 0.0,
                    tier=tier, extra={"retry_after": retry_after})
                payload["success"] = False
                yield ("result", _finalize(payload))
                return
            yield ("thinking", {
                "stage": "token_governor", "status": "ok",
                "remaining_tokens": remaining})

        # ── Stage 1: memory recall (L1+) ────────────────────────────
        memory_block = ""
        if AXELR_COGNITION_LEVEL >= 1 and user_id:
            yield ("thinking", {"stage": "memory", "status": "running"})
            memory_success = False
            try:
                try:
                    from ..core.memory import get_memory  # type: ignore
                except Exception:
                    from core.memory import get_memory  # type: ignore
                mem = await get_memory()
                hits = await asyncio.wait_for(
                    mem.recall(user_id=user_id, query=prompt, top_k=6),
                    timeout=_ms(MEMORY_BUDGET_MS))
                if hits:
                    memory_block = "\n".join(h.text for h in hits[:6])
                    self.stats["memory_hits"] += 1
                    memory_success = True
            except asyncio.TimeoutError:
                logger.warning("pipeline_memory_timeout - continuing")
            except Exception as e:
                logger.warning(
                    "pipeline_memory_error - continuing error=%s",
                    str(e)[:100])
            yield ("thinking", {
                "stage": "memory",
                "status": "ok" if memory_success else "skipped"})

        if memory_block:
            context = (
                context + "\n\nRelevant history:\n" + memory_block).strip()

        hist_key = "|".join(
            f"{m.get('role', 'u')}:{str(m.get('text', ''))[:64]}"
            for m in (history or [])[-2:] if isinstance(m, dict)
        ) if history else ""
        cache_prompt = (
            f"{hist_key}\nCurrent request: {prompt}"
            if hist_key else prompt)

        # ── Main loop ────────────────────────────────────────────────
        tried: set[str] = set()
        for round_num in range(1, MAX_ROUNDS + 1):
            if time.monotonic() >= deadline:
                break
            self.stats["rounds"] += 1
            relaxed = round_num >= 2

            # Stage 2: semantic cache (L1+)
            if AXELR_COGNITION_LEVEL >= 1:
                hit = await self._try_cache(cache_prompt, round_num)
                if hit is not None:
                    yield ("thinking", {
                        "stage": "cache", "status": "hit",
                        "round": round_num})
                    yield ("result", _finalize(
                        _pack(hit, "semantic_cache", "cache", 0.0,
                              tier="cached_similar"),
                        extra={"cached": True, "round": round_num}))
                    return
                yield ("thinking", {
                    "stage": "cache", "status": "miss",
                    "round": round_num})

            # Stage 3: wide vectorize (L2+, round >= 2)
            if AXELR_COGNITION_LEVEL >= 2 and round_num >= 2:
                hit = await self._try_cache(
                    cache_prompt, round_num, force_reembed=True)
                if hit is not None:
                    yield ("thinking", {
                        "stage": "vectorize", "status": "hit",
                        "round": round_num})
                    yield ("result", _finalize(
                        _pack(hit, "semantic_cache", "vector-match", 0.0,
                              tier="cached_similar"),
                        extra={"cached": True, "round": round_num}))
                    return
                yield ("thinking", {
                    "stage": "vectorize", "status": "miss",
                    "round": round_num})

            # Stage 4: remote providers (L1+)
            if (AXELR_COGNITION_LEVEL >= 1
                    and deadline - time.monotonic() > 0.4):
                res = await self._try_providers(
                    workspace=workspace, task_type=task_type,
                    prompt=prompt, history=history, context=context,
                    max_tokens=max_tokens, temp=temp,
                    deadline=deadline, skip=tried, relaxed=relaxed)
                if res is not None:
                    yield ("thinking", {
                        "stage": "providers", "status": "ok",
                        "winner": res["provider"],
                        "model": res["model_used"],
                        "round": round_num})
                    yield ("result", _finalize(res))
                    return
                yield ("thinking", {
                    "stage": "providers", "status": "all_failed",
                    "round": round_num})

            # Stage 5: LLM gateway (L2+)
            if (AXELR_COGNITION_LEVEL >= 2
                    and deadline - time.monotonic() > 0.5):
                res = await self._try_gateway(
                    prompt=prompt, max_tokens=max_tokens, temp=temp,
                    deadline=deadline, round_num=round_num)
                if res is not None:
                    yield ("thinking", {
                        "stage": "gateway", "status": "ok",
                        "round": round_num})
                    yield ("result", _finalize(res))
                    return

            # Stage 6: local twin (L3 only)
            if (AXELR_COGNITION_LEVEL >= 3
                    and deadline - time.monotonic() > 0.4):
                res = await self._try_local(
                    workspace, task_type, prompt, deadline, round_num,
                    context=context, tier=tier)
                if res is not None:
                    if context and context.strip():
                        yield ("thinking", {
                            "stage": "local", "status": "healing",
                            "recovered_bytes": len(context)})
                    yield ("thinking", {
                        "stage": "local", "status": "ok",
                        "round": round_num})
                    yield ("result", _finalize(res))
                    return
                yield ("thinking", {
                    "stage": "local", "status": "fail",
                    "round": round_num})

        # ── Stage 7: emergency stub ─────────────────────────────────
        self.stats["stub_fallbacks"] += 1
        elapsed = time.monotonic() - started
        logger.critical(
            "pipeline_exhausted_all_attempts user=%s elapsed=%.1fs tier=%s",
            user_id, elapsed, tier)

        try:
            try:
                from ..api.providers import (  # type: ignore
                    build_local_fallback_response,
                )
            except Exception:
                from api.providers import (  # type: ignore
                    build_local_fallback_response,
                )
        except Exception:
            def build_local_fallback_response(  # type: ignore
                w: str, t: str, p: str) -> str:
                return ""

        base_text = build_local_fallback_response(
            workspace, task_type, prompt)
        usable = bool(base_text and len(base_text.strip()) > 10)

        if elapsed >= deadline_s:
            text = base_text if usable else (
                "⚠️ High traffic detected. Routing you to our "
                "multi-agent system. Please wait a moment.")
            provider_label, model_label = (
                "multi-agent-router", "orchestrator")
            extra_flag = {"routing_to_multi_agent": True}
        else:
            text = base_text if usable else (
                "⚠️ All AI services are temporarily busy.")
            provider_label, model_label = ("local-fallback", "local")
            extra_flag = {"last_ditch": True}

        payload = _pack(
            text, provider_label, model_label, 0.0,
            tier="stub", extra=extra_flag)
        yield ("thinking", {"stage": "fallback", "status": "emergency"})
        yield ("result", _finalize(payload))

    # ── run / generate ──────────────────────────────────────────────
    async def run(self, **kwargs: Any) -> dict[str, Any]:
        """Collapse ``stream()`` to the single result payload. Never raises."""
        last: dict[str, Any] = {
            "success": False, "text": "", "provider": "none",
            "model_used": "none", "tokens_used": 0, "latency_ms": 0,
            "tier": "stub", "degraded": True,
        }
        async for kind, payload in self.stream(**kwargs):
            if kind == "result":
                last = payload
        return last

    async def generate(self, **kwargs: Any) -> dict[str, Any]:
        """Alias for ``run()``."""
        return await self.run(**kwargs)

    # ── stage impls ─────────────────────────────────────────────────
    async def _try_cache(
        self, prompt: str, round_num: int, *,
        force_reembed: bool = False,
    ) -> str | None:
        """Semantic-cache lookup. Returns cached text or ``None``. Never raises."""
        try:
            try:
                from ..api.state import state  # type: ignore
            except Exception:
                from api.state import state  # type: ignore
        except Exception:
            return None
        cache = getattr(state, "semantic_cache", None)
        if cache is None:
            return None
        budget = _ms(
            VECTORIZE_BUDGET_MS if force_reembed else CACHE_BUDGET_MS)
        try:
            hit = await asyncio.wait_for(
                cache.get(prompt), timeout=budget)
            if hit:
                self.stats["cache_hits"] += 1
            return hit
        except asyncio.TimeoutError:
            logger.debug("pipeline_cache_timeout round=%d", round_num)
            return None
        except Exception as e:
            logger.debug(
                "pipeline_cache_error round=%d error=%s", round_num, e)
            return None

    async def _try_providers(
        self, *, workspace: str, task_type: str, prompt: str,
        history: list[dict[str, Any]] | None, context: str,
        max_tokens: int, temp: float, deadline: float,
        skip: set[str], relaxed: bool,
    ) -> dict[str, Any] | None:
        """Heartbeat-ranked wave race, then sequential fallback. Never raises."""
        try:
            try:
                from ..api.prompts import _get_system_prompt  # type: ignore
                from ..api.providers import (  # type: ignore
                    PROVIDER_FUNC_MAP, PROVIDER_KEY_CHECK, PROVIDER_MODELS,
                    get_dynamically_ranked_providers,
                )
                from ..api.state import state  # type: ignore
            except Exception:
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
            recent = [
                f"{m.get('role', 'user')}: {m.get('text', '')}"
                for m in history[-4:]
                if isinstance(m, dict) and m.get("text")
            ]
            if recent:
                full_prompt += (
                    "Previous conversation:\n" + "\n".join(recent) + "\n\n")
        full_prompt += f"User request: {prompt}"

        try:
            ranked = await get_dynamically_ranked_providers(workspace)
        except Exception:
            ranked = list(PROVIDER_FUNC_MAP.keys())

        try:
            from .heartbeat import rank as _hb_rank
            ranked = _hb_rank(ranked)
        except Exception:
            pass

        def _eligible(p: str) -> bool:
            return (
                p != "local" and p not in skip
                and PROVIDER_KEY_CHECK.get(p, False)
                and bool(PROVIDER_MODELS.get(p))
            )

        available = [p for p in ranked if _eligible(p)]
        race_budget = _ms(RACE_BUDGET_MS)

        # Wave race
        while (available and RACE_WIDTH > 0
               and time.monotonic() < deadline):
            wave = available[:RACE_WIDTH]
            available = available[RACE_WIDTH:]
            if not relaxed:
                cb = getattr(state, "circuit_breaker", None)
                if cb is not None:
                    filtered: list[str] = []
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
                PROVIDER_FUNC_MAP, PROVIDER_MODELS,
                timeout_s=race_budget)
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
                        timeout=min(
                            PROVIDER_ATTEMPT_TIMEOUT_S,
                            max(0.5, seq_deadline - time.monotonic())))
                    if resp and len(resp.strip()) > 3:
                        self.stats["provider_wins"] += 1
                        skip.add(p)
                        return _pack(
                            resp, p, model,
                            (time.monotonic() - t0) * 1000.0)
                except Exception as e:
                    logger.debug(
                        "pipeline_provider_fail provider=%s attempt=%d error=%s",
                        p, attempt + 1, e)
                    await asyncio.sleep(0.15 * (attempt + 1))
            skip.add(p)
        return None

    async def _race(
        self, wave: list[str], full_prompt: str,
        max_tokens: int, temp: float,
        func_map: dict[str, Callable[..., Awaitable[str]]],
        models_map: dict[str, list[str]], *,
        timeout_s: float,
    ) -> dict[str, Any] | None:
        """Race up to ``len(wave)`` providers; first non-``None`` wins."""
        tasks: list[asyncio.Task[dict[str, Any] | None]] = []
        for p in wave:
            func = func_map.get(p)
            models = models_map.get(p) or []
            if not func or not models:
                continue
            tasks.append(asyncio.create_task(
                self._one_provider(
                    p, models[0], func, full_prompt, max_tokens, temp),
                name=f"race::{p}"))
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
    async def _one_provider(
        name: str, model: str,
        func: Callable[..., Awaitable[str]],
        prompt: str, max_tokens: int, temp: float,
    ) -> dict[str, Any] | None:
        """One provider attempt. Returns packed result or ``None``."""
        t0 = time.monotonic()
        try:
            resp = await func(prompt, max_tokens, temp, model)
            if resp and len(resp.strip()) > 3:
                return _pack(
                    resp, name, model,
                    (time.monotonic() - t0) * 1000.0)
        except Exception:
            return None
        return None

    async def _try_gateway(
        self, *, prompt: str, max_tokens: int, temp: float,
        deadline: float, round_num: int,
    ) -> dict[str, Any] | None:
        """LLM-gateway fallback. Returns packed result or ``None``. Never raises."""
        try:
            try:
                from ..api.state import state  # type: ignore
            except Exception:
                from api.state import state  # type: ignore
        except Exception:
            return None
        url = getattr(state, "gateway_url", None)
        key = getattr(state, "gateway_key", None)
        client = getattr(state, "gateway_http", None)
        if not url or client is None:
            return None
        budget = min(
            _ms(GATEWAY_BUDGET_MS), max(0.5, deadline - time.monotonic()))
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        body = {
            "model": "auto",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temp,
            "stream": False,
        }
        t0 = time.monotonic()
        try:
            r = await asyncio.wait_for(
                client.post(
                    f"{url.rstrip('/')}/v1/chat/completions",
                    headers=headers, json=body),
                timeout=budget)
            r.raise_for_status()
            data = r.json()
            text = (data.get("choices", [{}])[0]
                    .get("message", {}).get("content") or "").strip()
            if not text or _is_stub(text):
                return None
            self.stats["gateway_wins"] += 1
            return _pack(
                text, "llm_gateway", "gateway",
                (time.monotonic() - t0) * 1000.0)
        except (asyncio.TimeoutError, Exception) as e:
            logger.debug(
                "pipeline_gateway_fail round=%d error=%s", round_num, e)
            return None

    async def _try_local(
        self, workspace: str, task_type: str, prompt: str,
        deadline: float, round_num: int, *,
        context: str = "", tier: str = "free",
    ) -> dict[str, Any] | None:
        """Local-twin fallback. Returns packed result or ``None``. Never raises.

        ``context`` carries the Stream-Heal buffer so a timed-out remote
        stream resumes locally with tokens already emitted. ``tier`` is
        forwarded so the twin engine can enforce its own budget.
        """
        try:
            from .twin_engine import execute_local_inference
        except Exception:
            return None
        budget = min(
            _ms(HOT_SWAP_BUDGET_MS), max(0.3, deadline - time.monotonic()))
        full_prompt = f"{context}\n\n{prompt}" if context else prompt
        try:
            text = await asyncio.wait_for(
                execute_local_inference(
                    workspace, task_type, full_prompt, tier=tier),
                timeout=budget)
        except asyncio.TimeoutError:
            logger.warning("pipeline_local_timeout round=%d", round_num)
            return None
        except Exception as e:
            logger.warning(
                "pipeline_local_error round=%d error=%s", round_num, e)
            return None
        if not text or _is_stub(text):
            return None
        self.stats["hot_swap_wins"] += 1
        return _pack(
            text, "local_twin", "twin", budget * 1000.0, tier="local")


# ── Backward compatibility ────────────────────────────────────────────
# ``ProviderCascade`` was the historical name. Singletons (``get_pipeline``,
# ``run_pipeline``, ``stream_pipeline``) and ``get_hot_swap_detector``
# live in ``core.orchestration`` to keep this module pure.
ProviderCascade = ResilientPipeline


__all__ = [
    "ResilientPipeline", "ProviderCascade",
    "AXELR_COGNITION_LEVEL",
    "PIPELINE_DEADLINE_S", "FAST_TIER_DEADLINE_S", "PRO_TIER_DEADLINE_S",
    "MAX_ROUNDS", "RACE_WIDTH",
]