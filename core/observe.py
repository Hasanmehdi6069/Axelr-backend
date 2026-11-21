# core/observe.py
"""
AXELR — Observability Layer (trace · span · log · meter · tuner)
=================================================================
Everything a 3am page needs, behind one import:

    from core.observe import TRACES, trace_scope, METER, span, bind

Four concerns, one file — they share the same reader, the same
audit() shape, and the same "never raise on the hot path" contract:

    §1  TraceRing          in-process ring of the last N requests
    §2  OTLP spans         cross-pod correlation via OTLP HTTP
    §3  Log correlation    request_id → structlog contextvars
    §4  Provider metrics   Prometheus histograms keyed by provider/model
    §5  Meter              unified provider-health tracker (replaces
                           the old heartbeat + wave_racer + 3 _Health)
    §6  Memory tuner       gc.collect(1) + malloc_trim(0) per request

Bounded: 128 traces, 64 scopes, 128 providers/scope, 32 samples each.
"""

from __future__ import annotations

import contextlib
import contextvars
import ctypes
import gc
import logging
import os
import sys
import threading
import time
import uuid
from collections import OrderedDict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

logger = logging.getLogger("axelr.observe")


# ═══════════════════════════════════════════════════════════════════════════
# §1 — TraceRing
# ═══════════════════════════════════════════════════════════════════════════
_RING_SIZE = int(os.getenv("AXELR_TRACE_RING_SIZE", "128"))
_RING_ENABLED = _RING_SIZE > 0


@dataclass(slots=True)
class _Trace:
    rid: str
    workspace: str
    intent: str
    started_at: float
    events: list[dict[str, Any]] = field(default_factory=list)
    finished: bool = False
    status: str = "running"
    summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rid": self.rid,
            "workspace": self.workspace,
            "intent": self.intent,
            "started_at": self.started_at,
            "duration_ms": round((time.monotonic() - self.started_at) * 1000, 2),
            "status": self.status,
            "events": self.events[-64:],
            "summary": self.summary,
        }


class TraceRing:
    __slots__ = ("_by_id", "_enabled", "_lock", "_ring")

    def __init__(self, size: int) -> None:
        self._enabled = size > 0
        self._ring: deque[_Trace] = deque(maxlen=max(1, size))
        self._by_id: dict[str, _Trace] = {}
        self._lock = threading.Lock()

    def enabled(self) -> bool:
        return self._enabled

    def new_id(self) -> str:
        return uuid.uuid4().hex[:12]

    def begin(self, rid: str, workspace: str, intent: str) -> None:
        if not self._enabled:
            return
        with self._lock:
            t = _Trace(rid=rid, workspace=workspace, intent=intent, started_at=time.monotonic())
            self._ring.append(t)
            self._by_id[rid] = t

    def event(self, rid: str, name: str, **fields: Any) -> None:
        if not self._enabled:
            return
        with self._lock:
            t = self._by_id.get(rid)
            if t is None or t.finished:
                return
            t.events.append(
                {
                    "t_ms": round((time.monotonic() - t.started_at) * 1000, 2),
                    "name": name,
                    **{k: v for k, v in fields.items() if v is not None},
                }
            )

    def finish(self, rid: str, status: str, **summary: Any) -> None:
        if not self._enabled:
            return
        with self._lock:
            t = self._by_id.get(rid)
            if t is None:
                return
            t.finished = True
            t.status = status
            t.summary = summary
            if len(self._by_id) > max(256, _RING_SIZE * 2):
                live = {x.rid for x in self._ring}
                for k in list(self._by_id):
                    if k not in live:
                        self._by_id.pop(k, None)

    def last(self, n: int = 20) -> list[dict[str, Any]]:
        if not self._enabled:
            return []
        with self._lock:
            return [t.to_dict() for t in list(self._ring)[-n:]]

    def get(self, rid: str) -> dict[str, Any] | None:
        if not self._enabled:
            return None
        with self._lock:
            t = self._by_id.get(rid)
            return t.to_dict() if t else None


TRACES = TraceRing(_RING_SIZE)


@contextmanager
def trace_scope(rid: str, workspace: str, intent: str, **summary: Any):
    TRACES.begin(rid, workspace, intent)
    t0 = time.monotonic()
    try:
        yield rid
        TRACES.finish(rid, "ok", duration_ms=(time.monotonic() - t0) * 1000, **summary)
    except Exception as e:
        TRACES.finish(rid, "error", error=str(e)[:200], duration_ms=(time.monotonic() - t0) * 1000, **summary)
        raise


# ═══════════════════════════════════════════════════════════════════════════
# §2 — OTLP spans
# ═══════════════════════════════════════════════════════════════════════════
_OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
_OTLP_SERVICE = os.getenv("OTEL_SERVICE_NAME", "axelr")
_OTLP_QUEUE_CAP = int(os.getenv("AXELR_OTEL_QUEUE_CAP", "2000"))
_OTLP_PARENT_LRU = 256
_OTLP_ENABLED = bool(_OTLP_ENDPOINT)
_OTLP_OK = False
_tracer = None
_ot_trace = None

try:
    if _OTLP_ENABLED:
        from opentelemetry import trace as _ot_trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        _provider = TracerProvider(resource=Resource.create({"service.name": _OTLP_SERVICE}))
        _provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=_OTLP_ENDPOINT, timeout=5),
                max_queue_size=_OTLP_QUEUE_CAP,
                max_export_batch_size=256,
                schedule_delay_millis=2000,
                export_timeout_millis=5000,
            )
        )
        _ot_trace.set_tracer_provider(_provider)
        _tracer = _ot_trace.get_tracer("axelr")
        _OTLP_OK = True
except Exception as e:
    logger.warning("otel_sdk_unavailable error=%s", e)
    _OTLP_OK = False

_OTLP_PARENTS: "OrderedDict[str, Any]" = OrderedDict()
_otlp_lock = threading.Lock()
_otlp_stats = {"spans": 0, "stages": 0}


def _coerce(v: Any) -> str | int | float | bool:
    if isinstance(v, (str, int, float, bool)):
        return v
    return str(v)[:200]


def _otlp_parent(rid: str):
    with _otlp_lock:
        p = _OTLP_PARENTS.get(rid)
        if p is not None:
            _OTLP_PARENTS.move_to_end(rid)
            return p
        if not _OTLP_OK or _tracer is None:
            return None
        p = _tracer.start_span(f"request:{rid}")
        _OTLP_PARENTS[rid] = p
        if len(_OTLP_PARENTS) > _OTLP_PARENT_LRU:
            _, old = _OTLP_PARENTS.popitem(last=False)
            with contextlib.suppress(Exception):
                old.end()
        return p


@contextmanager
def span(rid: str, name: str, **attrs: Any) -> Iterator[None]:
    if not _OTLP_OK or _tracer is None:
        yield
        return
    parent = _otlp_parent(rid)
    ctx = _ot_trace.set_span_in_context(parent) if parent else None
    sp = _tracer.start_span(name, context=ctx) if ctx else _tracer.start_span(name)
    for k, v in attrs.items():
        with contextlib.suppress(Exception):
            sp.set_attribute(f"axelr.{k}", _coerce(v))
    _otlp_stats["spans"] += 1
    try:
        yield
    except Exception as e:
        with contextlib.suppress(Exception):
            sp.set_attribute("axelr.error", _coerce(str(e)))
            sp.record_exception(e)
        raise
    finally:
        with contextlib.suppress(Exception):
            sp.end()


def record_stage(rid: str, stage: str, duration_ms: float, **attrs: Any) -> None:
    if not _OTLP_OK:
        return
    with span(rid, f"stage:{stage}", duration_ms=round(duration_ms, 3), **attrs):
        _otlp_stats["stages"] += 1


def finish(rid: str, status: str = "ok", **attrs: Any) -> None:
    if not _OTLP_OK:
        return
    with _otlp_lock:
        p = _OTLP_PARENTS.pop(rid, None)
    if p is None:
        return
    with contextlib.suppress(Exception):
        p.set_attribute("axelr.status", _coerce(status))
        for k, v in attrs.items():
            p.set_attribute(f"axelr.{k}", _coerce(v))
    with contextlib.suppress(Exception):
        p.end()


def flush_pending_spans() -> int:
    if not _OTLP_OK:
        return 0
    with _otlp_lock:
        parents = list(_OTLP_PARENTS.values())
        _OTLP_PARENTS.clear()
    for p in parents:
        with contextlib.suppress(Exception):
            p.end()
    return len(parents)


# ═══════════════════════════════════════════════════════════════════════════
# §3 — Log correlation
# ═══════════════════════════════════════════════════════════════════════════
try:
    from structlog.contextvars import (
        bind_contextvars,
        get_contextvars,
        unbind_contextvars,
    )

    _STRUCTLOG = True
except Exception:
    _STRUCTLOG = False
    _fallback: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("axelr_log_ctx", default={})

    def bind_contextvars(**kw: Any) -> None:
        d = dict(_fallback.get())
        d.update(kw)
        _fallback.set(d)

    def unbind_contextvars(*keys: str) -> None:
        d = dict(_fallback.get())
        for k in keys:
            d.pop(k, None)
        _fallback.set(d)

    def get_contextvars() -> dict[str, Any]:
        return dict(_fallback.get())


_LOG_HEADER = "x-request-id"


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def bind(rid: str | None = None, **extra: Any) -> Any:
    rid = (rid or "").strip() or new_id()
    return bind_contextvars(request_id=rid, **extra)


def unbind(_token: Any = None) -> None:
    ctx = get_contextvars()
    if ctx:
        unbind_contextvars(*tuple(ctx.keys()))


def current_id() -> str:
    return str(get_contextvars().get("request_id") or "")


async def log_middleware(request, call_next):
    rid = (request.headers.get(_LOG_HEADER) or "").strip() or new_id()
    token = bind(rid, path=request.url.path, method=request.method)
    try:
        response = await call_next(request)
        response.headers[_LOG_HEADER] = rid
        return response
    finally:
        unbind(token)


# ═══════════════════════════════════════════════════════════════════════════
# §4 — Provider metrics (Prometheus)
# ═══════════════════════════════════════════════════════════════════════════
try:
    from prometheus_client import REGISTRY, Histogram

    def _metric(name: str, factory):
        try:
            return factory()
        except ValueError:
            return getattr(REGISTRY, "_names_to_collectors", {}).get(name)

    _LATENCY = _metric(
        "axelr_provider_latency_seconds",
        lambda: Histogram(
            "axelr_provider_latency_seconds",
            "Provider call latency",
            ["provider", "model", "outcome"],
            buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
        ),
    )
    _TTFT = _metric(
        "axelr_provider_ttft_seconds",
        lambda: Histogram(
            "axelr_provider_ttft_seconds",
            "Time to first token",
            ["provider", "model"],
            buckets=(0.01, 0.025, 0.05, 0.1, 0.2, 0.4, 0.8, 1.6, 3.2, 6.4),
        ),
    )
    _PROM_OK = True
except Exception as e:
    logger.warning("prometheus_unavailable error=%s", e)
    _LATENCY = _TTFT = None
    _PROM_OK = False


def _lbl(v: Any) -> str:
    return ("" if v is None else str(v))[:80]


def observe_call(provider: str, model: str, outcome: str, seconds: float) -> None:
    if _LATENCY is None:
        return
    with contextlib.suppress(Exception):
        _LATENCY.labels(provider=_lbl(provider), model=_lbl(model), outcome=_lbl(outcome)).observe(
            max(0.0, float(seconds))
        )


def observe_ttft(provider: str, model: str, seconds: float) -> None:
    if _TTFT is None:
        return
    with contextlib.suppress(Exception):
        _TTFT.labels(provider=_lbl(provider), model=_lbl(model)).observe(max(0.0, float(seconds)))


# ═══════════════════════════════════════════════════════════════════════════
# §5 — Meter (unified provider-health tracker)
# ═══════════════════════════════════════════════════════════════════════════
_SAMPLE_RING = 32
_MAX_PROVIDERS_PER_SCOPE = 128
_MAX_SCOPES = 64
_EWMA_ALPHA = 0.3


@dataclass(slots=True)
class _Stat:
    successes: int = 0
    failures: int = 0
    ewma_latency_ms: float = 0.0
    last_ts: float = 0.0
    samples: deque[float] = field(default_factory=lambda: deque(maxlen=_SAMPLE_RING))

    def record(self, success: bool, latency_ms: float) -> None:
        self.last_ts = time.monotonic()
        if success:
            self.successes += 1
        else:
            self.failures += 1
        latency_ms = max(0.0, float(latency_ms))
        self.samples.append(latency_ms)
        self.ewma_latency_ms = (
            latency_ms
            if self.ewma_latency_ms <= 0
            else _EWMA_ALPHA * latency_ms + (1 - _EWMA_ALPHA) * self.ewma_latency_ms
        )

    def success_rate(self) -> float:
        n = self.successes + self.failures
        return self.successes / n if n else 0.5

    def p(self, quantile: float) -> float:
        if not self.samples:
            return 0.0
        ordered = sorted(self.samples)
        idx = min(len(ordered) - 1, int(quantile * (len(ordered) - 1)))
        return ordered[idx]

    def health(self) -> float:
        """0.0–1.0. Weighted: 70% success rate, 30% inverse-EWMA-latency."""
        rate = self.success_rate()
        lat_penalty = 1.0 / (1.0 + self.ewma_latency_ms / 800.0)
        return round(0.7 * rate + 0.3 * lat_penalty, 4)


class Meter:
    __slots__ = ("_lock", "_scopes")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._scopes: OrderedDict[str, dict[str, _Stat]] = OrderedDict()

    def _scope(self, scope: str) -> dict[str, _Stat]:
        bucket = self._scopes.get(scope)
        if bucket is None:
            bucket = {}
            self._scopes[scope] = bucket
            if len(self._scopes) > _MAX_SCOPES:
                self._scopes.popitem(last=False)
        else:
            self._scopes.move_to_end(scope)
        return bucket

    def record(
        self, scope: str, provider: str, *, success: bool, latency_ms: float, ttft_ms: float | None = None
    ) -> None:
        if not scope or not provider:
            return
        with self._lock:
            bucket = self._scope(scope)
            stat = bucket.get(provider)
            if stat is None:
                stat = _Stat()
                bucket[provider] = stat
                if len(bucket) > _MAX_PROVIDERS_PER_SCOPE:
                    bucket.pop(next(iter(bucket)))
            stat.record(success, latency_ms)
            if ttft_ms is not None and ttft_ms > 0:
                stat.record(success, ttft_ms)

    def p50(self, scope: str, provider: str) -> float:
        with self._lock:
            stat = self._scopes.get(scope, {}).get(provider)
            return stat.p(0.50) if stat else 0.0

    def p95(self, scope: str, provider: str) -> float:
        with self._lock:
            stat = self._scopes.get(scope, {}).get(provider)
            return stat.p(0.95) if stat else 0.0

    def health(self, scope: str, provider: str) -> float:
        with self._lock:
            stat = self._scopes.get(scope, {}).get(provider)
            return stat.health() if stat else 0.5

    def rank(self, scope: str, providers: list[str]) -> list[str]:
        """Stable sort by descending health, then ascending p95."""
        if not providers:
            return []
        with self._lock:
            bucket = self._scopes.get(scope, {})
            scored: list[tuple[float, float, str]] = []
            for p in providers:
                stat = bucket.get(p)
                h = stat.health() if stat else 0.5
                p95 = stat.p(0.95) if stat else 800.0
                scored.append((-h, p95, p))
        scored.sort()
        return [p for _, _, p in scored]

    def snapshot(self, scope: str | None = None) -> dict[str, Any]:
        with self._lock:
            if scope is not None:
                bucket = self._scopes.get(scope, {})
                return {"scope": scope, "providers": {p: self._stat_dict(s) for p, s in bucket.items()}}
            out: dict[str, Any] = {}
            for scope_name, bucket in self._scopes.items():
                out[scope_name] = {p: self._stat_dict(s) for p, s in bucket.items()}
            return out

    @staticmethod
    def _stat_dict(s: _Stat) -> dict[str, float | int]:
        return {
            "successes": s.successes,
            "failures": s.failures,
            "success_rate": round(s.success_rate(), 3),
            "ewma_ms": round(s.ewma_latency_ms, 1),
            "p50_ms": round(s.p(0.50), 1),
            "p95_ms": round(s.p(0.95), 1),
            "health": s.health(),
            "samples": len(s.samples),
        }


METER = Meter()


# ═══════════════════════════════════════════════════════════════════════════
# §6 — Memory tuner (per-request gc.collect(1) + malloc_trim(0))
# ═══════════════════════════════════════════════════════════════════════════
_ENABLE_TUNER = os.getenv("AXELR_MEMORY_TUNER", "true").lower() in ("1", "true", "yes")
_LIBC_NAME = "libc.so.6" if sys.platform.startswith("linux") else None

_armed = False
_libc: ctypes.CDLL | None = None
_request_counter = 0
_tuner_stats: dict[str, int] = {"armed": 0, "trims": 0, "gc1_collected": 0, "libc_loaded": 0}


def _load_libc() -> ctypes.CDLL | None:
    if not _LIBC_NAME:
        return None
    try:
        lib = ctypes.CDLL(_LIBC_NAME, use_errno=False)
        lib.malloc_trim.argtypes = [ctypes.c_int]
        lib.malloc_trim.restype = ctypes.c_int
        _tuner_stats["libc_loaded"] = 1
        return lib
    except Exception as e:
        logger.debug("memory_tuner_libc_unavailable err=%s", e)
        return None


def arm() -> None:
    """Disable cyclic GC and load libc handle. Call once at startup."""
    global _armed, _libc
    if _armed or not _ENABLE_TUNER:
        return
    with contextlib.suppress(Exception):
        gc.disable()
    _libc = _load_libc()
    _armed = True
    _tuner_stats["armed"] = 1
    logger.info("memory_tuner_armed libc=%s", bool(_libc))


def disarm() -> None:
    """Re-enable cyclic GC. Call from shutdown."""
    global _armed
    if not _armed:
        return
    with contextlib.suppress(Exception):
        gc.enable()
        gc.collect(2)
    _armed = False


def _trim_sync() -> int:
    collected = 0
    with contextlib.suppress(Exception):
        collected = gc.collect(1)
    _tuner_stats["gc1_collected"] += collected
    if _libc is not None:
        with contextlib.suppress(Exception):
            _libc.malloc_trim(0)
            _tuner_stats["trims"] += 1
    return collected


async def trim_middleware(request, call_next):
    """Per-request trim. Runs after response generation."""
    global _request_counter
    response = await call_next(request)
    if not _armed or not _ENABLE_TUNER:
        return response
    _request_counter += 1
    every_n = max(1, int(os.getenv("AXELR_TRIM_EVERY_N", "1")))
    if _request_counter % every_n == 0:
        import asyncio

        with contextlib.suppress(Exception):
            await asyncio.to_thread(_trim_sync)
    return response


def tuner_stats() -> dict[str, int]:
    return dict(_tuner_stats)


# ═══════════════════════════════════════════════════════════════════════════
# Audit
# ═══════════════════════════════════════════════════════════════════════════
def audit() -> dict[str, Any]:
    return {
        "trace_ring": {"enabled": _RING_ENABLED, "size": _RING_SIZE},
        "otlp": {
            "enabled": _OTLP_OK,
            "endpoint": _OTLP_ENDPOINT or None,
            "service": _OTLP_SERVICE,
            "queue_cap": _OTLP_QUEUE_CAP,
            "parents_live": len(_OTLP_PARENTS),
            "spans": _otlp_stats["spans"],
        },
        "log_correlation": {"structlog": _STRUCTLOG, "header": _LOG_HEADER},
        "provider_metrics": {"enabled": _PROM_OK},
        "meter_scopes": len(METER._scopes),
        "memory_tuner": tuner_stats(),
    }


__all__ = [
    # Trace
    "TraceRing",
    "TRACES",
    "trace_scope",
    # OTLP
    "span",
    "record_stage",
    "finish",
    "flush_pending_spans",
    # Logs
    "bind",
    "unbind",
    "current_id",
    "new_id",
    "log_middleware",
    # Metrics
    "observe_call",
    "observe_ttft",
    # Meter
    "Meter",
    "METER",
    # Memory tuner
    "arm",
    "disarm",
    "trim_middleware",
    "tuner_stats",
    # Audit
    "audit",
]
