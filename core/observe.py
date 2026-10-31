# core/observe.py
"""
AXELR — Observability Layer (trace · span · log · metric)
==========================================================
Everything a 3am page needs, behind one import:

    from core.observe import TRACES, span, bind as bind_log, observe_call

Four concerns, one file — because they share the same reader, the same
audit() shape, and the same "never raise on the hot path" contract:

    §1 TraceRing         in-process ring of the last N requests
    §2 OTLP spans        cross-pod correlation via OTLP HTTP
    §3 Log correlation   request_id → structlog contextvars
    §4 Provider metrics  Prometheus histograms keyed by provider/model
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import time
import uuid
from collections import OrderedDict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock
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
            "rid": self.rid, "workspace": self.workspace, "intent": self.intent,
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
        self._lock = Lock()

    def enabled(self) -> bool: return self._enabled
    def new_id(self) -> str: return uuid.uuid4().hex[:12]

    def begin(self, rid: str, workspace: str, intent: str) -> None:
        if not self._enabled: return
        with self._lock:
            t = _Trace(rid=rid, workspace=workspace, intent=intent,
                       started_at=time.monotonic())
            self._ring.append(t)
            self._by_id[rid] = t

    def event(self, rid: str, name: str, **fields: Any) -> None:
        if not self._enabled: return
        with self._lock:
            t = self._by_id.get(rid)
            if t is None or t.finished: return
            t.events.append({
                "t_ms": round((time.monotonic() - t.started_at) * 1000, 2),
                "name": name,
                **{k: v for k, v in fields.items() if v is not None},
            })

    def finish(self, rid: str, status: str, **summary: Any) -> None:
        if not self._enabled: return
        with self._lock:
            t = self._by_id.get(rid)
            if t is None: return
            t.finished = True
            t.status = status
            t.summary = summary
            if len(self._by_id) > max(256, _RING_SIZE * 2):
                live = {x.rid for x in self._ring}
                for k in list(self._by_id):
                    if k not in live:
                        self._by_id.pop(k, None)

    def last(self, n: int = 20) -> list[dict[str, Any]]:
        if not self._enabled: return []
        with self._lock:
            return [t.to_dict() for t in list(self._ring)[-n:]]

    def get(self, rid: str) -> dict[str, Any] | None:
        if not self._enabled: return None
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
        TRACES.finish(rid, "ok",
                      duration_ms=(time.monotonic() - t0) * 1000, **summary)
    except Exception as e:
        TRACES.finish(rid, "error", error=str(e)[:200],
                      duration_ms=(time.monotonic() - t0) * 1000, **summary)
        raise


# ═══════════════════════════════════════════════════════════════════════════
# §2 — OTLP span emitter
# ═══════════════════════════════════════════════════════════════════════════
_OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
_OTLP_SERVICE = os.getenv("OTEL_SERVICE_NAME", "axelr")
_OTLP_QUEUE_CAP = int(os.getenv("AXELR_OTEL_QUEUE_CAP", "2000"))
_OTLP_PARENT_LRU = 256
_OTLP_ENABLED = bool(_OTLP_ENDPOINT)

try:
    if _OTLP_ENABLED:
        from opentelemetry import trace as _ot_trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        _OTLP_OK = True
    else:
        _OTLP_OK = False
except Exception as e:
    logger.warning("otel_sdk_unavailable error=%s", e)
    _OTLP_OK = False

if _OTLP_OK:
    _provider = TracerProvider(resource=Resource.create({"service.name": _OTLP_SERVICE}))
    _provider.add_span_processor(BatchSpanProcessor(
        OTLPSpanExporter(endpoint=_OTLP_ENDPOINT, timeout=5),
        max_queue_size=_OTLP_QUEUE_CAP, max_export_batch_size=256,
        schedule_delay_millis=2000, export_timeout_millis=5000,
    ))
    _ot_trace.set_tracer_provider(_provider)
    _tracer = _ot_trace.get_tracer("axelr")
else:
    _tracer = None

_otlp_parents: "OrderedDict[str, Any]" = OrderedDict()
_otlp_lock = Lock()
_otlp_stats = {"spans": 0, "stages": 0}


def _coerce(v: Any) -> str | int | float | bool:
    if isinstance(v, (str, int, float, bool)):
        return v
    return str(v)[:200]


def _otlp_parent(rid: str):
    with _otlp_lock:
        p = _otlp_parents.get(rid)
        if p is not None:
            _otlp_parents.move_to_end(rid)
            return p
        if not _OTLP_OK:
            return None
        p = _tracer.start_span(f"request:{rid}")
        _otlp_parents[rid] = p
        if len(_otlp_parents) > _OTLP_PARENT_LRU:
            _, old = _otlp_parents.popitem(last=False)
            with contextlib.suppress(Exception):
                old.end()
        return p


@contextmanager
def span(rid: str, name: str, **attrs: Any) -> Iterator[None]:
    if not _OTLP_OK:
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
        p = _otlp_parents.pop(rid, None)
    if p is None:
        return
    with contextlib.suppress(Exception):
        p.set_attribute("axelr.status", _coerce(status))
        for k, v in attrs.items():
            p.set_attribute(f"axelr.{k}", _coerce(v))
    with contextlib.suppress(Exception):
        p.end()


# ═══════════════════════════════════════════════════════════════════════════
# §3 — Log correlation
# ═══════════════════════════════════════════════════════════════════════════
try:
    from structlog.contextvars import (
        bind_contextvars, get_contextvars, unbind_contextvars,
    )
    _STRUCTLOG = True
except Exception:
    _STRUCTLOG = False
    _fallback: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
        "axelr_log_ctx", default={},
    )

    def bind_contextvars(**kw: Any) -> None:
        d = dict(_fallback.get()); d.update(kw); _fallback.set(d)

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
# §4 — Provider metrics
# ═══════════════════════════════════════════════════════════════════════════
try:
    from prometheus_client import REGISTRY, Histogram

    def _metric(name: str, factory):
        try:
            return factory()
        except ValueError:
            return getattr(REGISTRY, "_names_to_collectors", {}).get(name)

    _LATENCY = _metric("axelr_provider_latency_seconds", lambda: Histogram(
        "axelr_provider_latency_seconds", "Provider call latency",
        ["provider", "model", "outcome"],
        buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    ))
    _TTFT = _metric("axelr_provider_ttft_seconds", lambda: Histogram(
        "axelr_provider_ttft_seconds", "Time to first token",
        ["provider", "model"],
        buckets=(0.01, 0.025, 0.05, 0.1, 0.2, 0.4, 0.8, 1.6, 3.2, 6.4),
    ))
    _PROM_OK = True
except Exception as e:
    logger.warning("prometheus_unavailable error=%s", e)
    _LATENCY = _TTFT = None
    _PROM_OK = False


def _lbl(v: Any) -> str:
    return ("" if v is None else str(v))[:80]


def observe_call(provider: str, model: str, outcome: str, seconds: float) -> None:
    if _LATENCY is None: return
    with contextlib.suppress(Exception):
        _LATENCY.labels(provider=_lbl(provider), model=_lbl(model),
                        outcome=_lbl(outcome)).observe(max(0.0, float(seconds)))


def observe_ttft(provider: str, model: str, seconds: float) -> None:
    if _TTFT is None: return
    with contextlib.suppress(Exception):
        _TTFT.labels(provider=_lbl(provider), model=_lbl(model)) \
             .observe(max(0.0, float(seconds)))

def flush_pending_spans() -> int:
    """End every pending parent span. Returns count flushed. Never raises."""
    if not _OTLP_OK:
        return 0
    with _otlp_lock:
        parents = list(_otlp_parents.values())
        _otlp_parents.clear()
    for p in parents:
        with contextlib.suppress(Exception):
            p.end()
    return len(parents)
# ═══════════════════════════════════════════════════════════════════════════
# Audit
# ═══════════════════════════════════════════════════════════════════════════
def audit() -> dict[str, Any]:
    return {
        "trace_ring": {"enabled": _RING_ENABLED, "size": _RING_SIZE},
        "otlp": {"enabled": _OTLP_OK, "endpoint": _OTLP_ENDPOINT or None,
                 "service": _OTLP_SERVICE, "queue_cap": _OTLP_QUEUE_CAP,
                 "parents_live": len(_otlp_parents),
                 "spans": _otlp_stats["spans"]},
        "log_correlation": {"structlog": _STRUCTLOG, "header": _LOG_HEADER},
        "provider_metrics": {"enabled": _PROM_OK},
    }


__all__ = [
    # trace ring
    "TraceRing", "TRACES", "trace_scope",
    # otlp
    "span", "record_stage", "finish", "flush_pending_spans",
    # log
    "bind", "unbind", "current_id", "new_id", "log_middleware",
    # provider metrics
    "observe_call", "observe_ttft",
    # audit
    "audit",
]