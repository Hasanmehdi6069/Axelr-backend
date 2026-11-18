# core/config.py
"""
AXELR — Typed Configuration
=============================
Replaces ~150 scattered os.getenv() calls with one frozen Settings object.
Reads the same env vars; adds validation + a single audit() surface.

Design: pure stdlib. No pydantic required. Read once at import.
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from typing import Any

def _int(key: str, default: int) -> int:
    try: return int(os.getenv(key, str(default)))
    except (TypeError, ValueError): return default

def _float(key: str, default: float) -> float:
    try: return float(os.getenv(key, str(default)))
    except (TypeError, ValueError): return default

def _bool(key: str, default: bool = False) -> bool:
    v = (os.getenv(key, "") or "").lower()
    if not v: return default
    return v in ("1", "true", "yes", "on")

def _str(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


@dataclass(frozen=True, slots=True)
class Settings:
    # ── Pipeline ──────────────────────────────────────────────
    deadline_s:             float = field(default_factory=lambda: _float("AXELR_CONDUCTOR_DEADLINE_S", 25.0))
    max_extra_calls:        int   = field(default_factory=lambda: _int("AXELR_CONDUCTOR_MAX_CALLS", 3))
    cognition_level:        int   = field(default_factory=lambda: _int("AXELR_COGNITION_LEVEL", 2))
    watermark:              bool  = field(default_factory=lambda: _bool("AXELR_WATERMARK", True))
    # ── TTC ───────────────────────────────────────────────────
    ttc_enabled:            bool  = field(default_factory=lambda: _bool("AXELR_ENABLE_TTC", False))
    ttc_max_width:          int   = field(default_factory=lambda: _int("AXELR_TTC_MAX_WIDTH", 3))
    # ── Thermal ───────────────────────────────────────────────
    thermal_interval_s:     float = field(default_factory=lambda: _float("AXELR_THERMAL_INTERVAL_S", 5.0))
    thermal_warm_pct:       float = field(default_factory=lambda: _float("AXELR_THERMAL_WARM_PCT", 70.0))
    thermal_hot_pct:        float = field(default_factory=lambda: _float("AXELR_THERMAL_HOT_PCT", 85.0))
    thermal_critical_pct:   float = field(default_factory=lambda: _float("AXELR_THERMAL_CRITICAL_PCT", 95.0))
    # ── Micro-volatile ────────────────────────────────────────
    micro_volatile_mb:      int   = field(default_factory=lambda: _int("AXELR_MICRO_VOLATILE_MB", 15))
    micro_volatile_tokens:  int   = field(default_factory=lambda: _int("AXELR_MICRO_VOLATILE_MAX_TOKENS", 128))
    micro_volatile_chars:   int   = field(default_factory=lambda: _int("AXELR_MICRO_VOLATILE_MAX_CHARS", 800))
    # ── Override timer ────────────────────────────────────────
    override_seconds:       int   = field(default_factory=lambda: _int("AXELR_OVERRIDE_SECONDS", 5))
    override_ttl_s:         int   = field(default_factory=lambda: _int("AXELR_OVERRIDE_TTL_S", 300))
    # ── Burst scheduler ───────────────────────────────────────
    burst_default_min:      float = field(default_factory=lambda: _float("AXELR_BURST_DEFAULT_MIN", 10.0))
    burst_hard_max_min:     float = field(default_factory=lambda: _float("AXELR_BURST_HARD_MAX_MIN", 20.0))
    burst_max_active:       int   = field(default_factory=lambda: _int("AXELR_BURST_MAX_ACTIVE", 4))
    # ── Prewarm ───────────────────────────────────────────────
    prewarm_enabled:        bool  = field(default_factory=lambda: _bool("AXELR_PREWARM", True))
    prewarm_cooldown_s:     float = field(default_factory=lambda: _float("AXELR_PREWARM_COOLDOWN_S", 90.0))
    prewarm_max_concurrent: int   = field(default_factory=lambda: _int("AXELR_PREWARM_MAX_CONCURRENT", 4))
    prewarm_timeout_s:      float = field(default_factory=lambda: _float("AXELR_PREWARM_TIMEOUT_S", 6.0))
    # ── Memory tuner ──────────────────────────────────────────
    memory_tuner:           bool  = field(default_factory=lambda: _bool("AXELR_MEMORY_TUNER", True))
    memory_threshold_mb:    int   = field(default_factory=lambda: _int("AXELR_MEMORY_THRESHOLD_MB", 500))
    memory_panic_mb:        int   = field(default_factory=lambda: _int("AXELR_MEMORY_PANIC_MB", 750))
    memory_sample_interval_s: float = field(default_factory=lambda: _float("AXELR_MEMORY_SAMPLE_INTERVAL_S", 1.0))
    memory_gc_cooldown_s:   float = field(default_factory=lambda: _float("AXELR_MEMORY_GC_COOLDOWN_S", 30.0))
    memory_max_threads:     int   = field(default_factory=lambda: _int("AXELR_MEMORY_MAX_THREADS", 40))
    trim_every_n:           int   = field(default_factory=lambda: max(1, _int("AXELR_TRIM_EVERY_N", 1)))
    # ── Circuit breaker ───────────────────────────────────────
    cb_threshold:           int   = field(default_factory=lambda: _int("AXELR_CB_THRESHOLD", 3))
    cb_cooldown_s:          float = field(default_factory=lambda: _float("AXELR_CB_COOLDOWN_S", 60.0))
    cb_rate_limit_cooldown: float = field(default_factory=lambda: _float("AXELR_CB_RATE_LIMIT_COOLDOWN_S", 1800.0))
    # ── Meter ─────────────────────────────────────────────────
    meter_sample_ring:      int   = field(default_factory=lambda: _int("AXELR_METER_SAMPLE_RING", 32))
    # ── Conductor: feature gates ──────────────────────────────
    enable_memo:            bool  = field(default_factory=lambda: _bool("AXELR_CONDUCTOR_MEMO", True))
    enable_bilingual:       bool  = field(default_factory=lambda: _bool("AXELR_CONDUCTOR_BILINGUAL", True))
    enable_consensus:       bool  = field(default_factory=lambda: _bool("AXELR_CONDUCTOR_CONSENSUS", True))
    enable_split:           bool  = field(default_factory=lambda: _bool("AXELR_CONDUCTOR_SPLIT", False))
    enable_hedge:           bool  = field(default_factory=lambda: _bool("AXELR_ELITE_HEDGE", True))
    enable_neg_cache:       bool  = field(default_factory=lambda: _bool("AXELR_ELITE_NEG_CACHE", True))
    enable_health_prop:     bool  = field(default_factory=lambda: _bool("AXELR_ELITE_HEALTH_PROP", True))
    enable_token_trim:      bool  = field(default_factory=lambda: _bool("AXELR_ELITE_TOKEN_TRIM", True))
    enable_canonical_pfx:   bool  = field(default_factory=lambda: _bool("AXELR_ELITE_CANONICAL_PREFIX", True))
    # ── Local router / verify ─────────────────────────────────
    local_router_mode:      str   = field(default_factory=lambda: _str("AXELR_LOCAL_ROUTER", "off").lower())
    local_verify_mode:      str   = field(default_factory=lambda: _str("AXELR_LOCAL_VERIFY", "off").lower())
    # ── Twin ──────────────────────────────────────────────────
    twin_max_prompt_chars:  int   = field(default_factory=lambda: _int("TWIN_MAX_PROMPT_CHARS", 2400))
    twin_fast_parallel:     int   = field(default_factory=lambda: _int("TWIN_FAST_PARALLEL", 2))
    # Elite models are NEVER run in render - set elite parallelism to 0
    twin_elite_parallel:    int   = field(default_factory=lambda: _int("TWIN_ELITE_PARALLEL", 0))
    twin_fast_timeout_s:    float = field(default_factory=lambda: _float("TWIN_FAST_TIMEOUT_S", 35.0))
    # Hard render memory ceiling enforced: 450MB max, 380MB preferred total
    twin_elite_timeout_s:   float = field(default_factory=lambda: _float("TWIN_ELITE_TIMEOUT_S", 90.0))
    twin_rss_soft_cap_mb:   int   = field(default_factory=lambda: _int("TWIN_RSS_SOFT_CAP_MB", 380))  # Preferred total (system + twin engine)
    twin_rss_hard_cap_mb:   int   = field(default_factory=lambda: _int("TWIN_RSS_HARD_CAP_MB", 450))  # Hard ceiling - never exceed
    twin_dual_min_ram_mb:   int   = field(default_factory=lambda: _int("TWIN_DUAL_MIN_RAM_MB", 0))  # Disable dual elite/fast mode
    # ── Gateway ───────────────────────────────────────────────
    gateway_timeout_ms:     int   = field(default_factory=lambda: _int("GATEWAY_TIMEOUT_MS", 4000))
    gateway_cooldown_s:     float = field(default_factory=lambda: _float("GATEWAY_COOLDOWN_S", 45.0))
    gateway_max_cooldown_s: float = field(default_factory=lambda: _float("GATEWAY_MAX_COOLDOWN_S", 600.0))
    # ── Governor ──────────────────────────────────────────────
    tok_free_min:           int   = field(default_factory=lambda: _int("TOK_FREE_MIN", 25_000))
    tok_free_day:           int   = field(default_factory=lambda: _int("TOK_FREE_DAY", 300_000))
    tok_pro_min:            int   = field(default_factory=lambda: _int("TOK_PRO_MIN", 120_000))
    tok_pro_day:            int   = field(default_factory=lambda: _int("TOK_PRO_DAY", 2_000_000))
    tok_biz_min:            int   = field(default_factory=lambda: _int("TOK_BIZ_MIN", 400_000))
    tok_biz_day:            int   = field(default_factory=lambda: _int("TOK_BIZ_DAY", 8_000_000))
    # ── BM25 ──────────────────────────────────────────────────
    bm25_max_docs:          int   = field(default_factory=lambda: _int("AXELR_BM25_MAX_DOCS", 5000))
    bm25_max_chars:         int   = field(default_factory=lambda: _int("AXELR_BM25_MAX_CHARS", 8000))


_CACHED: Settings | None = None


def load() -> Settings:
    """Return the process-wide Settings. Cached after first call."""
    global _CACHED
    if _CACHED is None:
        _CACHED = Settings()
    return _CACHED


def reload() -> Settings:
    """Force a fresh read. Useful for tests."""
    global _CACHED
    _CACHED = None
    return load()


def audit() -> dict[str, Any]:
    s = load()
    return {"version": "1.0",
            "settings": {k: getattr(s, k) for k in s.__dataclass_fields__}}


# ── Adapter initialization & status tracking ──────────────────────────────────
import logging
import asyncio
from typing import Any, Dict

logger = logging.getLogger("axelr.core.config")
_ADAPTER_STATUSES: Dict[str, Dict[str, Any]] = {}

async def initialize_all() -> None:
    """Initialize all registered adapters and providers (Appwrite, PayPal, Vultr).
    Follows native module imports to avoid mixing project structure.
    """
    logger.info("starting_all_adapter_initialization")
    _ADAPTER_STATUSES.clear()

    # Initialize Appwrite (insights_engine) adapter
    try:
        from insights_engine.core import init as init_appwrite
        await init_appwrite()
        _ADAPTER_STATUSES["appwrite"] = {"healthy": True, "initialized_at": asyncio.get_event_loop().time()}
        logger.info("appwrite_adapter_initialized_successfully")
    except Exception as e:
        _ADAPTER_STATUSES["appwrite"] = {"healthy": False, "error": str(e)}
        logger.error("appwrite_adapter_initialization_failed", error=str(e))

    # Initialize PayPal adapter
    try:
        from axelr_paypal.core import init as init_paypal
        await init_paypal()
        _ADAPTER_STATUSES["paypal"] = {"healthy": True, "initialized_at": asyncio.get_event_loop().time()}
        logger.info("paypal_adapter_initialized_successfully")
    except Exception as e:
        _ADAPTER_STATUSES["paypal"] = {"healthy": False, "error": str(e)}
        logger.error("paypal_adapter_initialization_failed", error=str(e))

    # Initialize Vultr adapter
    try:
        from axelr_vultr.core import init as init_vultr
        await init_vultr()
        _ADAPTER_STATUSES["vultr"] = {"healthy": True, "initialized_at": asyncio.get_event_loop().time()}
        logger.info("vultr_adapter_initialized_successfully")
    except Exception as e:
        _ADAPTER_STATUSES["vultr"] = {"healthy": False, "error": str(e)}
        logger.error("vultr_adapter_initialization_failed", error=str(e))

    logger.info("all_adapters_initialization_complete", total_adapters=len(_ADAPTER_STATUSES), healthy_adapters=sum(1 for s in _ADAPTER_STATUSES.values() if s["healthy"]))

def get_adapter_statuses() -> Dict[str, Dict[str, Any]]:
    """Return a shallow copy of all adapter health statuses for /api/health/ready endpoint."""
    # Return a copy to prevent external modification of internal state
    return {k: v.copy() for k, v in _ADAPTER_STATUSES.items()}


__all__ = ["Settings", "load", "reload", "audit", "initialize_all", "get_adapter_statuses"]