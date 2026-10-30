"""Backend protocol + shared helpers + resilience primitives.

This module is the seam between the audit/search façades and the two
storage implementations. It intentionally contains **no storage-specific
code** — no SDK calls, no queries — so it can be imported by both
`storage_mongo` and `storage_appwrite` without circular imports.

What lives here
---------------
* The `Backend` protocol — the contract every implementation honours.
* Serialization helpers (`to_iso`, `to_dt`, `serialize`, `_denorm`).
* Percentile + pipeline-stats math shared by both backends.
* `CircuitBreaker` — used by `AppwriteBackend._call` as the single
  choke point for SDK failures. Not used by Mongo (PyMongo reconnects
  internally; adding a breaker there would only add latency).
* `RateLimiter` — per-key sliding window, consumed in `routes.py`.
* `init_backend / get_backend / close_backend` — factory. Imports the
  concrete backend lazily inside the factory to avoid circular imports.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from .config import settings

logger = logging.getLogger("insights.storage")


# ── Serialization ─────────────────────────────────────────────────────

def to_iso(dt: datetime) -> str:
    """UTC ISO-8601 with 'Z' suffix. Naive datetimes assumed UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def to_dt(value: Any) -> datetime | None:
    """Coerce strings/ints/datetimes to a UTC datetime, or None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def serialize(obj: Any) -> Any:
    """Recursively convert datetimes → ISO-8601 strings."""
    if isinstance(obj, datetime):
        return to_iso(obj)
    if isinstance(obj, dict):
        return {k: serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [serialize(v) for v in obj]
    return obj


def _denorm(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract hot fields into indexed columns for Appwrite.

    Appwrite has no object attribute and no server-side aggregation.
    Rather than query into a JSON blob, we promote the handful of fields
    that analytics actually filter/group on. Everything else stays in
    `payload`.
    """
    out: dict[str, Any] = {}
    for key in ("user_id", "pipeline_id", "status"):
        v = payload.get(key)
        if isinstance(v, str) and v:
            out[key] = v[:255]
    lat = payload.get("latency_ms")
    if isinstance(lat, (int, float)) and not isinstance(lat, bool):
        out["latency_ms"] = float(lat)
    ts = to_dt(payload.get("ts"))
    if ts is not None:
        out["day"] = to_iso(ts)[:10]
    return out


# ── The Protocol ──────────────────────────────────────────────────────

@runtime_checkable
class Backend(Protocol):
    """Storage contract. Both implementations must honour every method."""

    async def init(self) -> None: ...
    async def close(self) -> None: ...
    async def health(self) -> dict[str, Any]: ...

    # Audit writes
    async def insert_audit(self, kind: str, payload: dict[str, Any]) -> str | None: ...
    async def insert_audit_batch(self, events: list[dict[str, Any]]) -> int: ...

    # Audit reads
    async def list_audit(
        self, *, kind: str, limit: int = 50, offset: int = 0,
        user_id: str | None = None, pipeline_id: str | None = None,
        start: datetime | None = None, end: datetime | None = None,
    ) -> list[dict[str, Any]]: ...

    # Analytics
    async def latency_stats(
        self, *, pipeline_id: str | None, since: datetime,
    ) -> dict[str, Any]: ...

    async def pipeline_stats(
        self, *, since: datetime, limit: int = 50,
    ) -> list[dict[str, Any]]: ...

    async def daily_counts(self, *, since: datetime) -> list[dict[str, Any]]: ...
    async def latest_pipeline_state(
        self, *, pipeline_id: str,
    ) -> dict[str, Any] | None: ...
    async def purge_audit(self, *, older_than_days: int) -> int: ...

    # Documents
    async def upsert_document(
        self, doc_id: str, content: str, meta: dict[str, Any],
    ) -> dict[str, Any]: ...
    async def list_documents(self, limit: int = 200) -> list[dict[str, Any]]: ...
    async def delete_document(self, doc_id: str) -> bool: ...


# ── Shared math ───────────────────────────────────────────────────────

def _py_percentiles(vals: list[float], backend: str) -> dict[str, Any]:
    """Percentile stats computed in Python — used by Appwrite and Mongo fallback."""
    if not vals:
        return {"count": 0, "p50": None, "p95": None, "p99": None,
                "mean": None, "min": None, "max": None, "backend": backend}
    vals = sorted(vals)
    n = len(vals)

    def p(q: float) -> float:
        return vals[min(n - 1, int(round(q * (n - 1))))]

    return {
        "count": n,
        "p50": round(p(0.50), 2),
        "p95": round(p(0.95), 2),
        "p99": round(p(0.99), 2),
        "mean": round(sum(vals) / n, 2),
        "min": vals[0], "max": vals[-1],
        "backend": backend,
    }


def _finalise_pipeline_row(
    slot: dict[str, Any], lat: list[float],
) -> dict[str, Any]:
    """Normalise a pipeline aggregation row for the response shape."""
    total = int(slot.get("total") or 0)
    errors = int(slot.get("errors") or 0)
    return {
        "pipeline_id": slot.get("pipeline_id", "unknown"),
        "total": total,
        "errors": errors,
        "error_rate": round(errors / total, 4) if total else 0.0,
        "avg_latency_ms": (
            round(sum(lat) / len(lat), 2) if lat
            else slot.get("avg_latency_ms")
        ),
        "max_latency_ms": max(lat) if lat else slot.get("max_latency_ms"),
        "health_status": (
            "healthy" if (errors / total if total else 0) < 0.05
            else "degraded"
        ),
    }


# ── Circuit breaker ───────────────────────────────────────────────────

class CircuitOpenError(RuntimeError):
    """Raised when the circuit is open and the backend should not be called."""


class CircuitBreaker:
    """Trip after `threshold` consecutive failures; cool down for `cooldown`s.

    States:
      closed     — calls flow. `failures` counts consecutive failures.
      open       — calls fail fast. `opened_at` records when.
      half-open  — after cooldown, the next call is allowed as a probe.
                    One success → closed. One failure → open again.

    This is standard Nygard-style; ~40 lines of logic, no ambiguity.
    """

    def __init__(self, threshold: int = 10, cooldown: float = 60.0) -> None:
        self._threshold = threshold
        self._cooldown = cooldown
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = asyncio.Lock()

    async def allow(self) -> bool:
        async with self._lock:
            if self._opened_at is None:
                return True
            if time.monotonic() - self._opened_at >= self._cooldown:
                # Half-open: allow one probe.
                self._opened_at = None
                self._failures = 0
                return True
            return False

    async def record_success(self) -> None:
        async with self._lock:
            self._failures = 0
            self._opened_at = None

    async def record_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            if self._failures >= self._threshold and self._opened_at is None:
                self._opened_at = time.monotonic()
                logger.warning(
                    "circuit breaker opened failures=%d cooldown=%ss",
                    self._failures, self._cooldown,
                )

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None


# ── Rate limiter ──────────────────────────────────────────────────────

class RateLimiter:
    """Per-key sliding-window limiter.

    Honest limitation: this is in-process. Behind N uvicorn workers, each
    worker has its own bucket, so the effective limit is `max * N`. For
    a distributed limit, back this with Redis — swap `_buckets` for
    `redis.incr` with a TTL. Not done here because it would pull Redis
    into the critical path for every request.
    """

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(self, key: str) -> bool:
        now = time.monotonic()
        async with self._lock:
            bucket = self._buckets[key]
            while bucket and now - bucket[0] > self._window:
                bucket.popleft()
            if len(bucket) >= self._max:
                return False
            bucket.append(now)
            return True


# ── Factory ───────────────────────────────────────────────────────────

_backend: Backend | None = None
_backend_lock = asyncio.Lock()


async def init_backend() -> Backend:
    """Create and initialise the singleton backend. Idempotent.

    Imports the concrete backend lazily so a Mongo-only deployment does
    not need the Appwrite SDK installed, and vice-versa.
    """
    global _backend
    if _backend is not None:
        return _backend
    async with _backend_lock:
        if _backend is not None:
            return _backend

        if settings.storage_backend == "appwrite":
            if not settings.appwrite_configured():
                raise RuntimeError(
                    "storage_backend=appwrite requires APPWRITE_ENDPOINT, "
                    "APPWRITE_PROJECT_ID, APPWRITE_API_KEY"
                )
            from .storage_appwrite import AppwriteBackend
            b: Backend = AppwriteBackend(
                endpoint=settings.appwrite_endpoint,
                project=settings.appwrite_project_id,
                api_key=settings.appwrite_api_key,
                database_id=settings.appwrite_database_id,
            )
        else:
            if not settings.mongo_configured():
                raise RuntimeError("storage_backend=mongo requires MONGODB_URI")
            from .storage_mongo import MongoBackend
            b = MongoBackend(uri=settings.mongodb_uri, db_name=settings.mongodb_db)

        await b.init()
        _backend = b
        return b


def get_backend() -> Backend:
    if _backend is None:
        raise RuntimeError(
            "backend not initialised — call init_backend() during startup"
        )
    return _backend


async def close_backend() -> None:
    global _backend
    if _backend is not None:
        await _backend.close()
        _backend = None