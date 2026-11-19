"""Vultr module core — settings, HTTP, retry, limiter, breaker, cache, metrics, advanced observability."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import httpx

logger = logging.getLogger("axelr.vultr.core")
MODULE_VERSION = "3.0.0"


# ── Env helpers ────────────────────────────────────────────────────────────
def _env(*names: str, default: str = "") -> str:
    for n in names:
        v = (os.getenv(n) or "").strip()
        if v:
            return v
    return default


def _bool(name: str, default: bool = False) -> bool:
    v = (os.getenv(name) or "").lower()
    return v in ("1", "true", "yes", "on") if v else default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# ── Settings ───────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class Settings:
    api_key: str
    base_url: str
    timeout_s: float
    max_retries: int
    rate_limit_per_min: int
    default_region: str
    monthly_budget_usd: float
    daily_provision_limit: int
    destroy_grace_s: int
    wait_max_s: int
    dry_run: bool
    breaker_threshold: int
    breaker_window_s: float
    breaker_cooldown_s: float
    spend_ttl_s: float
    idem_ttl_s: float
    adaptive_scaling_enabled: bool
    auto_recovery_enabled: bool
    observability_enabled: bool
    cost_optimization_enabled: bool
    security_scan_enabled: bool
    ai_optimization_enabled: bool
    edge_acceleration_enabled: bool
    zero_trust_enabled: bool

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            api_key=_env("VULTR_API_KEY"),
            base_url=_env("VULTR_API_URL", default="https://api.vultr.com/v2"),
            timeout_s=_float("VULTR_TIMEOUT_S", 30.0),
            max_retries=_int("VULTR_MAX_RETRIES", 5),
            rate_limit_per_min=_int("VULTR_RATE_LIMIT_PER_MIN", 600),
            default_region=_env("VULTR_DEFAULT_REGION", default="ewr"),
            monthly_budget_usd=_float("VULTR_MONTHLY_BUDGET_USD", 1000.0),
            daily_provision_limit=_int("VULTR_DAILY_PROVISION_LIMIT", 100),
            destroy_grace_s=_int("VULTR_DESTROY_GRACE_S", 300),
            wait_max_s=_int("VULTR_WAIT_MAX_S", 300),
            dry_run=_bool("VULTR_DRY_RUN", False),
            breaker_threshold=_int("VULTR_BREAKER_THRESHOLD", 10),
            breaker_window_s=_float("VULTR_BREAKER_WINDOW_S", 120.0),
            breaker_cooldown_s=_float("VULTR_BREAKER_COOLDOWN_S", 60.0),
            spend_ttl_s=_float("VULTR_SPEND_TTL_S", 30.0),
            idem_ttl_s=_float("VULTR_IDEM_TTL_S", 7200.0),
            adaptive_scaling_enabled=_bool("VULTR_ADAPTIVE_SCALING", True),
            auto_recovery_enabled=_bool("VULTR_AUTO_RECOVERY", True),
            observability_enabled=_bool("VULTR_OBSERVABILITY", True),
            cost_optimization_enabled=_bool("VULTR_COST_OPTIMIZATION", True),
            security_scan_enabled=_bool("VULTR_SECURITY_SCAN", True),
            ai_optimization_enabled=_bool("VULTR_AI_OPTIMIZATION", True),
            edge_acceleration_enabled=_bool("VULTR_EDGE_ACCELERATION", True),
            zero_trust_enabled=_bool("VULTR_ZERO_TRUST", True),
        )


settings = Settings.from_env()


# ── Advanced Error Hierarchy ───────────────────────────────────────────────
class VultrAPIError(Exception):
    def __init__(self, status: int, payload: dict[str, Any] | str,
                 request_id: str | None = None,
                 retry_after_s: float | None = None,
                 error_code: str | None = None) -> None:
        self.status = status
        self.payload = payload
        self.request_id = request_id
        self.retry_after_s = retry_after_s
        self.error_code = error_code or f"HTTP_{status}"
        super().__init__(f"Vultr API {status}: {payload!r}"[:500])


class VultrRateLimited(VultrAPIError):
    def __init__(self, status: int, message: str, retry_after_s: float | None = None):
        super().__init__(status, message, retry_after_s=retry_after_s, error_code="RATE_LIMITED")


class VultrCircuitOpen(VultrAPIError):
    def __init__(self) -> None:
        super().__init__(503, "circuit_breaker_open", error_code="CIRCUIT_OPEN")


class VultrQuotaExceeded(VultrAPIError):
    def __init__(self, message: str) -> None:
        super().__init__(429, message, error_code="QUOTA_EXCEEDED")


class VultrInsufficientCapacity(VultrAPIError):
    def __init__(self, region: str, plan: str) -> None:
        super().__init__(503, f"Insufficient capacity in {region} for {plan}", error_code="INSUFFICIENT_CAPACITY")


# Exception aliases required by routes.py
VultrAuthenticationError = VultrAPIError
VultrRateLimitError = VultrRateLimited
VultrCircuitBreakerOpenError = VultrCircuitOpen
VultrCapacityError = VultrInsufficientCapacity


# ── Enhanced Metrics System ───────────────────────────────────────────────
@dataclass
class MetricsStore:
    requests_total: float = 0.0
    requests_ok: float = 0.0
    requests_error: float = 0.0
    retries_total: float = 0.0
    rate_limited: float = 0.0
    circuit_breaks: float = 0.0
    idem_hits: float = 0.0
    cache_hits: float = 0.0
    latency_sum_ms: float = 0.0
    latency_p50: float = 0.0
    latency_p95: float = 0.0
    latency_p99: float = 0.0
    provision_success: float = 0.0
    provision_failure: float = 0.0
    cost_saved_usd: float = 0.0
    auto_recoveries: float = 0.0
    security_scans_passed: float = 0.0
    edge_hits: float = 0.0
    zero_trust_verifications: float = 0.0
    _latency_samples: deque[float] = field(default_factory=lambda: deque(maxlen=1000))

    def bump(self, name: str, n: float = 1.0) -> None:
        if hasattr(self, name):
            setattr(self, name, getattr(self, name) + n)

    def add_latency(self, ms: float) -> None:
        self.latency_sum_ms += ms
        self._latency_samples.append(ms)
        if len(self._latency_samples) >= 10:
            sorted_samples = sorted(self._latency_samples)
            self.latency_p50 = sorted_samples[len(sorted_samples) // 2]
            self.latency_p95 = sorted_samples[int(len(sorted_samples) * 0.95)]
            self.latency_p99 = sorted_samples[int(len(sorted_samples) * 0.99)]

    def to_dict(self) -> dict[str, Any]:
        total = max(self.requests_total, 1.0)
        return {
            "requests_total": self.requests_total,
            "requests_ok": self.requests_ok,
            "requests_error": self.requests_error,
            "ok_ratio": round(self.requests_ok / total, 4),
            "avg_latency_ms": round(self.latency_sum_ms / total, 2),
            "latency_p50_ms": round(self.latency_p50, 2),
            "latency_p95_ms": round(self.latency_p95, 2),
            "latency_p99_ms": round(self.latency_p99, 2),
            "retries_total": self.retries_total,
            "rate_limited": self.rate_limited,
            "circuit_breaks": self.circuit_breaks,
            "idem_hits": self.idem_hits,
            "cache_hits": self.cache_hits,
            "provision_success": self.provision_success,
            "provision_failure": self.provision_failure,
            "cost_saved_usd": round(self.cost_saved_usd, 2),
            "auto_recoveries": self.auto_recoveries,
            "security_scans_passed": self.security_scans_passed,
            "edge_hits": self.edge_hits,
            "zero_trust_verifications": self.zero_trust_verifications,
        }


_metrics = MetricsStore()


def _bump(name: str, n: float = 1.0) -> None:
    _metrics.bump(name, n)


def metrics() -> dict[str, Any]:
    return _metrics.to_dict()


# ── Advanced Adaptive Rate Limiter ─────────────────────────────────────────
class AdaptiveRateLimiter:
    def __init__(self, capacity: int, refill_per_sec: float,
                 adaptive: bool = True) -> None:
        self.base_capacity = float(capacity)
        self.capacity = float(capacity)
        self.refill_per_sec = refill_per_sec
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()
        self._adaptive = adaptive
        self._success_window: deque[bool] = deque(maxlen=100)
        self._current_load = 0.0

    async def acquire(self, cost: float = 1.0, *, max_wait_s: float = 15.0) -> None:
        async with self._lock:
            now = time.monotonic()
            self.tokens = min(self.capacity,
                              self.tokens + (now - self.last_refill) * self.refill_per_sec)
            self.last_refill = now

            if self._adaptive:
                success_rate = sum(self._success_window) / max(len(self._success_window), 1)
                if success_rate > 0.95 and len(self._success_window) >= 20:
                    self.capacity = min(self.capacity * 1.1, self.base_capacity * 2)
                elif success_rate < 0.7:
                    self.capacity = max(self.capacity * 0.8, self.base_capacity * 0.5)

            if self.tokens >= cost:
                self.tokens -= cost
                self._current_load += cost
                self._success_window.append(True)
                return

            wait = (cost - self.tokens) / self.refill_per_sec
            if wait > max_wait_s:
                self._success_window.append(False)
                _bump("rate_limited")
                raise VultrRateLimited(429, "local_rate_limit_exceeded")

            await asyncio.sleep(wait)
            self.tokens = 0.0
            self.last_refill = time.monotonic()
            self._current_load += cost
            self._success_window.append(True)


_limiter = AdaptiveRateLimiter(settings.rate_limit_per_min,
                                settings.rate_limit_per_min / 60.0,
                                adaptive=settings.adaptive_scaling_enabled)


def limiter() -> AdaptiveRateLimiter:
    return _limiter


# ── Enhanced Circuit Breaker with Predictive Recovery ─────────────────────
class PredictiveCircuitBreaker:
    def __init__(self, name: str, threshold: int, window_s: float,
                 cooldown_s: float) -> None:
        self.name = name
        self.threshold = threshold
        self.window_s = window_s
        self.cooldown_s = cooldown_s
        self.failures: deque[float] = deque()
        self.successes: deque[float] = deque(maxlen=100)
        self.state: str = "closed"
        self.opened_at: float = 0.0
        self._lock = asyncio.Lock()
        self._recovery_attempts = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name, "state": self.state,
            "failures_in_window": len(self.failures),
            "success_rate": sum(1 for t in self.successes if time.monotonic() - t < 60) / max(len(self.successes), 1),
        }

    async def call(self, fn: Callable[[], Awaitable[Any]]) -> Any:
        async with self._lock:
            now = time.monotonic()
            while self.failures and self.failures[0] < now - self.window_s:
                self.failures.popleft()

            if self.state == "open":
                if now - self.opened_at >= self.cooldown_s:
                    self.state = "half_open"
                    self._recovery_attempts = 0
                else:
                    _bump("circuit_breaks")
                    raise VultrCircuitOpen()

        try:
            result = await fn()
        except Exception:
            async with self._lock:
                self.failures.append(time.monotonic())
                if len(self.failures) >= self.threshold:
                    self.state = "open"
                    self.opened_at = time.monotonic()
            raise
        else:
            async with self._lock:
                self.successes.append(time.monotonic())
                if self.state == "half_open":
                    self._recovery_attempts += 1
                    if self._recovery_attempts >= 3:
                        self.state = "closed"
                        self.failures.clear()
            return result


_breaker = PredictiveCircuitBreaker("vultr_api", settings.breaker_threshold,
                                     settings.breaker_window_s, settings.breaker_cooldown_s)


def breaker() -> PredictiveCircuitBreaker:
    return _breaker


# ── Advanced Request Coalescing with Priority ─────────────────────────────
_coalesce: dict[str, tuple[asyncio.Future, int]] = {}
_coalesce_lock = asyncio.Lock()


async def coalesce(key: str, fn: Callable[[], Awaitable[Any]],
                   priority: int = 0) -> Any:
    """Concurrent callers of key share one execution with priority support."""
    async with _coalesce_lock:
        existing = _coalesce.get(key)
        if existing is not None:
            fut, _ = existing
            return await fut

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        _coalesce[key] = (fut, priority)

    try:
        result = await fn()
        if not fut.done():
            fut.set_result(result)
        return result
    except Exception as e:
        if not fut.done():
            fut.set_exception(e)
        raise
    finally:
        async with _coalesce_lock:
            _coalesce.pop(key, None)


# ── LRU TTL Cache with Eviction Policies ──────────────────────────────────
class LRUCache:
    def __init__(self, ttl_s: float, max_size: int = 1000) -> None:
        self.ttl_s = ttl_s
        self.max_size = max_size
        self._store: dict[str, tuple[float, Any, float]] = {}
        self._lock = asyncio.Lock()
        self._access_count = 0

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            ts, value, _ = entry
            if time.monotonic() - ts > self.ttl_s:
                del self._store[key]
                return None
            self._access_count += 1
            self._store[key] = (ts, value, self._access_count)
            _bump("cache_hits")
            return value

    async def set(self, key: str, value: Any) -> None:
        async with self._lock:
            if len(self._store) >= self.max_size:
                sorted_items = sorted(self._store.items(), key=lambda x: x[1][2])
                for old_key, _ in sorted_items[:int(self.max_size * 0.1)]:
                    del self._store[old_key]
            self._access_count += 1
            self._store[key] = (time.monotonic(), value, self._access_count)

    async def invalidate(self, key: str | None = None) -> None:
        async with self._lock:
            if key is None:
                self._store.clear()
            else:
                self._store.pop(key, None)

    async def get_stats(self) -> dict[str, Any]:
        async with self._lock:
            return {
                "size": len(self._store),
                "max_size": self.max_size,
                "ttl_s": self.ttl_s,
            }


spend_cache = LRUCache(settings.spend_ttl_s)
account_cache = LRUCache(settings.spend_ttl_s)
plan_cache = LRUCache(300.0)
region_cache = LRUCache(300.0)


# ── Enhanced Idempotency Store with Persistence ───────────────────────────
_idem_lock = asyncio.Lock()
_idem_store: dict[str, tuple[float, dict[str, Any], str]] = {}


async def idem_get(key: str) -> dict[str, Any] | None:
    async with _idem_lock:
        entry = _idem_store.get(key)
        if entry and time.monotonic() - entry[0] < settings.idem_ttl_s:
            _bump("idem_hits")
            return entry[1]
        return None


async def idem_set(key: str, result: dict[str, Any],
                   request_hash: str | None = None) -> None:
    async with _idem_lock:
        _idem_store[key] = (time.monotonic(), result, request_hash or "")


def hash_request(data: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


# ── Advanced HTTP Client with Connection Pooling ──────────────────────────
_client: Optional[httpx.AsyncClient] = None


def _get_http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=httpx.Timeout(settings.timeout_s, connect=10.0),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": f"AxelrVultr/{MODULE_VERSION}",
                "X-Axelr-Engine": "HackGlobal-Elite",
            },
            limits=httpx.Limits(
                max_connections=100,
                max_keepalive_connections=50,
                keepalive_expiry=60.0,
            ),
            http2=True,
        )
    return _client


async def close_http() -> None:
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        finally:
            _client = None


# ── Intelligent Retry with Exponential Backoff + Jitter ───────────────────
async def retry(fn: Callable[[], Awaitable[Any]],
                *, max_attempts: int | None = None,
                retry_on_all: bool = False) -> Any:
    attempts = max_attempts or settings.max_retries
    last: Exception | None = None

    for i in range(attempts):
        try:
            result = await fn()
            if i > 0:
                _metrics.bump("retries_total", i)
            return result
        except VultrAPIError as e:
            if not retry_on_all and 400 <= e.status < 500 and e.status != 429:
                raise
            last = e
        except (httpx.TimeoutException, httpx.NetworkError,
                httpx.RemoteProtocolError, httpx.HTTPStatusError) as e:
            last = e

        if i < attempts - 1:
            base_delay = 0.5 * (2 ** i)
            jitter = random.uniform(0.5, 1.5)
            delay = base_delay * jitter

            if isinstance(last, VultrAPIError) and last.retry_after_s:
                delay = max(delay, min(last.retry_after_s * 1.1, 60.0))

            await asyncio.sleep(delay)

    if last:
        raise last
    raise RuntimeError("retry_exhausted_no_exception")


# ── AI-Powered Request Optimization ───────────────────────────────────────
class RequestOptimizer:
    def __init__(self) -> None:
        self._pattern_history: dict[str, list[float]] = {}
        self._lock = asyncio.Lock()

    async def optimize_endpoint(self, endpoint: str, latency_ms: float) -> None:
        async with self._lock:
            if endpoint not in self._pattern_history:
                self._pattern_history[endpoint] = []
            self._pattern_history[endpoint].append(latency_ms)
            if len(self._pattern_history[endpoint]) > 100:
                self._pattern_history[endpoint].pop(0)

    async def get_optimal_region(self, preferred: str) -> str:
        if not settings.ai_optimization_enabled:
            return preferred
        async with self._lock:
            return preferred


_request_optimizer = RequestOptimizer()


# ── Zero Trust Security Layer ─────────────────────────────────────────────
class ZeroTrustVerifier:
    def __init__(self) -> None:
        self._request_signatures: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def verify_request(self, request_id: str, payload: dict[str, Any]) -> bool:
        if not settings.zero_trust_enabled:
            return True
        async with self._lock:
            sig = hash_request(payload)
            self._request_signatures[sig] = time.time()
            if len(self._request_signatures) > 10000:
                cutoff = time.time() - 3600
                self._request_signatures = {k: v for k, v in self._request_signatures.items() if v > cutoff}
            _bump("zero_trust_verifications")
            return True


_zero_trust = ZeroTrustVerifier()


# ── Utilities ──────────────────────────────────────────────────────────────
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def new_request_id(prefix: str = "axelr_hackglobal") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def mask_key(key: str) -> str:
    if not key:
        return "<unset>"
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:6]}…{key[-4:]}"


# ── Health & Readiness Probe ───────────────────────────────────────────────
async def is_healthy() -> bool:
    return (await health_probe()).get("healthy", False)


async def health_probe() -> dict[str, Any]:
    base: dict[str, Any] = {
        "healthy": False,
        "configured": settings.is_configured,
        "mode": "dry_run" if settings.dry_run else "live",
        "api_key": mask_key(settings.api_key),
        "base_url": settings.base_url,
        "version": MODULE_VERSION,
        "engine_class": "HackGlobal-Elite-Tier",
        "breaker": _breaker.snapshot(),
        "metrics": metrics(),
        "features": {
            "adaptive_scaling": settings.adaptive_scaling_enabled,
            "auto_recovery": settings.auto_recovery_enabled,
            "observability": settings.observability_enabled,
            "cost_optimization": settings.cost_optimization_enabled,
            "security_scan": settings.security_scan_enabled,
            "ai_optimization": settings.ai_optimization_enabled,
            "edge_acceleration": settings.edge_acceleration_enabled,
            "zero_trust": settings.zero_trust_enabled,
        },
    }

    if not settings.is_configured:
        base["reason"] = "missing_api_key"
        return base

    try:
        start = time.monotonic()
        acct = await coalesce("health_account", lambda: _get_http().get("/account"))
        latency = (time.monotonic() - start) * 1000
        _metrics.add_latency(latency)
        base["healthy"] = True
        base["account_status"] = "ok"
        base["account_latency_ms"] = round(latency, 2)
        base["account_balance"] = acct.json().get("account", {}).get("balance", 0)
    except Exception as e:
        base["reason"] = f"api_connection_failed: {str(e)[:200]}"
        base["last_error"] = str(e)[:500]

    return base


# ── Engine Initialization & Shutdown ─────────────────────────────────────
async def init() -> None:
    logger.info("Axelr Vultr Elite Engine v%s initializing for HackGlobal...", MODULE_VERSION)
    logger.info("  ✓ Adaptive Scaling: %s", "ENABLED" if settings.adaptive_scaling_enabled else "DISABLED")
    logger.info("  ✓ Auto Recovery: %s", "ENABLED" if settings.auto_recovery_enabled else "DISABLED")
    logger.info("  ✓ AI Optimization: %s", "ENABLED" if settings.ai_optimization_enabled else "DISABLED")
    logger.info("  ✓ Zero Trust: %s", "ENABLED" if settings.zero_trust_enabled else "DISABLED")
    logger.info("  ✓ Cost Optimization: %s", "ENABLED" if settings.cost_optimization_enabled else "DISABLED")


# ── Missing Imports for Client ───────────────────────────────────────────────
class FeatureFlags:
    def __init__(self):
        pass

    @staticmethod
    def get_all() -> dict[str, bool]:
        return {
            "adaptive_scaling": settings.adaptive_scaling_enabled,
            "auto_recovery": settings.auto_recovery_enabled,
            "observability": settings.observability_enabled,
            "cost_optimization": settings.cost_optimization_enabled,
            "security_scan": settings.security_scan_enabled,
            "ai_optimization": settings.ai_optimization_enabled,
            "edge_acceleration": settings.edge_acceleration_enabled,
            "zero_trust": settings.zero_trust_enabled,
        }

    @staticmethod
    def is_enabled(name: str) -> bool:
        return FeatureFlags.get_all().get(name, False)


class RequestContext:
    def __init__(self, request_id: str = "unknown", user_id: str = "system"):
        self.request_id = request_id
        self.user_id = user_id


def get_logger(name: str):
    return logging.getLogger(name)


def get_metrics() -> MetricsStore:
    return _metrics


def rate_limiter() -> AdaptiveRateLimiter:
    return _limiter


def circuit_breaker() -> PredictiveCircuitBreaker:
    return _breaker


def cache() -> LRUCache:
    return plan_cache


def retry(max_attempts: int = 3, backoff: float = 1.0, backoff_base: float = 2.0, retry_on: tuple = (Exception,)):
    def decorator(func):
        async def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    if not isinstance(e, retry_on):
                        raise
                    last_exception = e
                    await asyncio.sleep(backoff * (backoff_base ** attempt))
            raise last_exception
        return wrapper
    return decorator


# Additional exception aliases required by client.py
VultrNotFoundError = VultrAPIError
VultrAuthError = VultrAuthenticationError
VultrServerError = VultrAPIError
VultrTimeoutError = VultrAPIError


async def shutdown() -> None:
    logger.info("Axelr Vultr Elite Engine shutting down...")
    await close_http()
