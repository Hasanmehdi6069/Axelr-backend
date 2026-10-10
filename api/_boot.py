# api/_boot.py
"""
AXELR — Boot Sequence — v2.3 (single consolidated install layer)
==================================================================
Exactly TWO operations. That is the whole file.

    1. ``core.elite_orchestrator.hedge_wrap``   — transport invariant
    2. ``_install_cognition``                   — decision invariant

Every other operational concern (stream gates, watermarks, token
governance, PII filters, prompt pruning, hyperparams) lives inside
``core.resilience.ResilientPipeline`` and is selected at request time
by ``AXELR_COGNITION_LEVEL``.

WIRE-UP
-------
  api/__init__.py — last executable line:  from . import _boot  # noqa
  api/warhead.py  — after include_router:  _boot.attach(app)
"""
from __future__ import annotations

import contextlib
import logging
import os
from typing import Any

logger = logging.getLogger("axelr.boot")

_VERSION = "2.3"
_INSTALLED: bool = False
_ATTACHED: set[int] = set()


def classify_intent(prompt: str) -> str:
    """Stub for the real intent classifier (canonical: core.context.classify_flat)."""
    try:
        from core.context import classify_flat
        return classify_flat(prompt)
    except Exception:
        if not prompt:
            return "conversational"
        return "conversational" if len(prompt.split()) <= 7 else "strategy"


# ── Consensus constants (env-gated, off by default) ─────────────────────
_CONSENSUS_ENABLED = os.getenv("AXELR_GOAT_CONSENSUS", "false").lower() in ("1", "true", "yes")
_CONSENSUS_WIDTH = max(2, min(3, int(os.getenv("AXELR_GOAT_CONSENSUS_WIDTH", "2"))))
_CONSENSUS_FLOOR = float(os.getenv("AXELR_GOAT_CONSENSUS_FLOOR", "0.55"))

_SSE_CONTRACT: dict[str, Any] = {
    "version": 2,
    "endpoints": {
        "/api/extract_stream": {
            "media_type": "text/event-stream",
            "frames": [
                {"event": "route",
                 "data": {"route": "CORE|DATA|DESIGN",
                          "intent": "conversational|strategy|transactional|"
                                    "extraction|styling|diagnostic"}},
                {"event": "thinking",
                 "data": {"stage": "sanitize|memory|cache|providers|local|fallback",
                          "status": "running|ok|hit|miss|all_failed|fail"}},
                {"event": "thinking",
                 "data": {"content": "<model reasoning>", "source": "llm"}},
                {"event": "message", "data": {"text": "<chunk>"}},
                {"event": "message", "data": {"feature_metadata": {}}},
                {"event": "message",
                 "data": {"watermark": "\\n\\n---\\n*<provider> · <model> · <t>s*"}},
                {"event": "close", "data": {}},
            ],
        },
    },
}


# ═══════════════════════════════════════════════════════════════════════════
# Operation 1 · transport hedge
# ═══════════════════════════════════════════════════════════════════════════
def _install_hedge_wrap() -> None:
    """
    Wire the hedge wrapper onto the elite orchestrator. Idempotent; if the
    orchestrator isn't importable at boot time (degraded mode), the wrap
    is attached by ``core.routing.Orchestrator`` on first use.
    """
    try:
        from . import routes_core as rc
        from core.elite_orchestrator import hedge_wrap
    except Exception as e:                                    # noqa: BLE001
        logger.info("hedge_wrap_deferred reason=%s", e)
        return
    current = getattr(rc, "route_ai_request_parallel", None)
    if current is None:
        logger.info("hedge_wrap_deferred reason=no_route_symbol")
        return
    if getattr(current, "_axelr_hedged", False):
        return
    with contextlib.suppress(Exception):
        wrapped = hedge_wrap(current)
        wrapped._axelr_hedged = True                          # type: ignore[attr-defined]
        rc.route_ai_request_parallel = wrapped
        for mod_name in ("routes_agents", "routes_tools", "ai_router"):
            try:
                mod = __import__(f"{__package__}.{mod_name}", fromlist=[mod_name])
                if hasattr(mod, "route_ai_request_parallel"):
                    mod.route_ai_request_parallel = wrapped
            except Exception:
                pass
        logger.info("hedge_wrap_installed")


# ═══════════════════════════════════════════════════════════════════════════
# Operation 2 · cognition pipeline
# ═══════════════════════════════════════════════════════════════════════════
def _install_cognition() -> None:
    """
    Install the unified cognition pipeline. All stage decisions (stream
    gate, watermark, token governance, PII filters) are made inside
    ``ResilientPipeline.stream`` based on ``AXELR_COGNITION_LEVEL``.

    This function does NOT monkey-patch anything. It ensures the
    ``ResilientPipeline`` singleton is constructed and its module-level
    cognition level is read before the first request is served.
    """
    try:
        from core.resilience import AXELR_COGNITION_LEVEL, get_pipeline
        get_pipeline()
        logger.info("cognition_pipeline_installed level=%d", AXELR_COGNITION_LEVEL)
    except Exception as e:                                    # noqa: BLE001
        logger.warning("cognition_pipeline_install_failed error=%s", e)


# ═══════════════════════════════════════════════════════════════════════════
# Install orchestrator — TWO operations only
# ═══════════════════════════════════════════════════════════════════════════
def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    logger.info("installing_boot_sequence version=%s", _VERSION)
    _install_hedge_wrap()
    _install_cognition()
    _INSTALLED = True
    logger.info("axelr_boot_install_complete version=%s", _VERSION)


def attach(app) -> None:
    """Attach contract + audit routes to the FastAPI app."""
    if id(app) in _ATTACHED:
        return
    _ATTACHED.add(id(app))

    try:
        if not any(getattr(r, "path", None) == "/api/contract"
                   for r in getattr(app, "routes", [])):
            @app.get("/api/contract", include_in_schema=False)
            async def _contract():
                return _SSE_CONTRACT
    except Exception as e:                                    # noqa: BLE001
        logger.debug("boot_contract_attach_failed error=%s", e)

    try:
        if not any(getattr(r, "path", None) == "/api/audit"
                   for r in getattr(app, "routes", [])):
            @app.get("/api/audit", include_in_schema=False)
            async def _audit_endpoint():
                return _audit_snapshot()
    except Exception as e:                                    # noqa: BLE001
        logger.debug("boot_audit_attach_failed error=%s", e)

    logger.debug("boot_attached_to_app")


def _audit_snapshot() -> dict[str, Any]:
    out: dict[str, Any] = {"version": _VERSION, "installed": _INSTALLED}
    try:
        from core.resilience import audit as _res_audit
        out["resilience"] = _res_audit()
    except Exception:
        out["resilience"] = {"error": "unavailable"}
    out["consensus"] = {
        "enabled": _CONSENSUS_ENABLED,
        "width": _CONSENSUS_WIDTH,
        "floor": _CONSENSUS_FLOOR,
    }
    out["cognition_pipeline"] = _INSTALLED
    return out


def audit() -> dict[str, Any]:
    return _audit_snapshot()


__all__ = ["install", "attach", "audit", "classify_intent"]


with contextlib.suppress(Exception):
    install()