# core/guards.py
"""
AXELR — Operational Guards (thermal · micro-volatile · burst · token)
======================================================================
Every operational budget enforcer in one file. All four share the same
lifecycle contract:

    * Bounded state (deques, TTL dicts, LRU caches).
    * Never raise to the caller.
    * Degrade silently when psutil / Redis are absent.
    * One audit() per section.

    §1  Thermal guard       live CPU/RAM/swap/IO sampling → pressure/budget
    §2  Micro-volatile      ≤15 MB / 128-token emergency ceiling
    §3  Override advisory   5 s countdown modal + cross-guard handoff
    §4  Burst scheduler     hard ≤20 min background-work window
    §5  Token governor      per-tier sliding-window token budget

Read-only module. All singletons are constructed lazily.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Callable

from .foundation import load as _cfg

logger = logging.getLogger("axelr.guards")

try:
    import psutil  # type: ignore[import-not-found]

    _HAS_PSUTIL = True
except ImportError:
    psutil = None  # type: ignore[assignment]
    _HAS_PSUTIL = False


# ═══════════════════════════════════════════════════════════════════════════
# §1 — Thermal guard
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(slots=True)
class _ThermalSnap:
    ts: float
    cpu_pct: float
    mem_pct: float
    swap_pct: float
    net_recv_mb: float
    net_sent_mb: float
    load1: float
    threads: int
    pressure: str
    budget: int


class _ThermalGuard:
    __slots__ = (
        "_cpu_ring",
        "_task",
        "_lock",
        "_running",
        "_snap",
        "_net_prev",
        "_consecutive_critical",
        "_swap_warned",
        "_net_io_warned",
    )

    def __init__(self) -> None:
        cfg = _cfg()
        cores = os.cpu_count() or 2
        self._lock = threading.Lock()
        self._cpu_ring: deque[float] = deque(maxlen=max(2, 12))
        self._net_prev = (0, 0)
        self._consecutive_critical = 0
        self._swap_warned = False
        self._net_io_warned = False
        self._running = False
        self._task: asyncio.Task | None = None
        self._snap = _ThermalSnap(
            ts=time.time(),
            cpu_pct=0.0,
            mem_pct=0.0,
            swap_pct=0.0,
            net_recv_mb=0.0,
            net_sent_mb=0.0,
            load1=0.0,
            threads=threading.active_count(),
            pressure="ok",
            budget=max(2, cores),
        )

    def _read_cpu(self) -> float:
        if not _HAS_PSUTIL:
            return 0.0
        try:
            return float(psutil.cpu_percent(interval=None))
        except Exception:
            return 0.0

    def _read_mem(self) -> float:
        if not _HAS_PSUTIL:
            return 0.0
        try:
            return float(psutil.virtual_memory().percent)
        except Exception:
            return 0.0

    def _read_swap(self) -> float:
        if not _HAS_PSUTIL:
            return 0.0
        try:
            return float(psutil.swap_memory().percent)
        except Exception:
            return 0.0

    def _read_net_io(self, interval_s: float) -> tuple[float, float]:
        if not _HAS_PSUTIL:
            return (0.0, 0.0)
        try:
            nc = psutil.net_io_counters()
            curr = (nc.bytes_recv, nc.bytes_sent)
            if not self._net_prev[0]:
                self._net_prev = curr
                return (0.0, 0.0)
            dr = (curr[0] - self._net_prev[0]) / (1024 * 1024) / interval_s
            ds = (curr[1] - self._net_prev[1]) / (1024 * 1024) / interval_s
            self._net_prev = curr
            return (dr, ds)
        except Exception:
            return (0.0, 0.0)

    def _read_load(self) -> float:
        try:
            return float(os.getloadavg()[0])
        except (OSError, AttributeError):
            return 0.0

    def _compute(self, interval_s: float) -> _ThermalSnap:
        cfg = _cfg()
        cpu = self._read_cpu()
        self._cpu_ring.append(cpu)
        worst = max(self._cpu_ring) if self._cpu_ring else cpu
        mem_pct = self._read_mem()
        swap_pct = self._read_swap()
        net_recv, net_sent = self._read_net_io(interval_s)
        load1 = self._read_load()
        n_threads = threading.active_count()
        cores = os.cpu_count() or 2
        pressure = "ok"
        budget = max(2, cores)

        if swap_pct >= cfg.thermal_swap_warn_pct and not self._swap_warned:
            logger.warning("thermal_swap_warning swap_pct=%.1f", swap_pct)
            self._swap_warned = True
        elif swap_pct < cfg.thermal_swap_warn_pct:
            self._swap_warned = False

        if (net_recv + net_sent) >= cfg.thermal_net_io_mb and not self._net_io_warned:
            logger.warning("thermal_net_io_warning total_mb_s=%.1f", net_recv + net_sent)
            self._net_io_warned = True
        elif (net_recv + net_sent) < cfg.thermal_net_io_mb:
            self._net_io_warned = False

        if worst >= cfg.thermal_critical_pct or load1 >= cores * 2.5:
            self._consecutive_critical += 1
            if self._consecutive_critical >= cfg.thermal_circuit_break:
                logger.critical("thermal_circuit_breaker_triggered consecutive=%d", self._consecutive_critical)
                pressure, budget = "critical", 0
            else:
                pressure, budget = "critical", 1
        else:
            self._consecutive_critical = 0
            if worst >= cfg.thermal_hot_pct or load1 >= cores * 1.5:
                pressure, budget = "hot", max(1, min(2, cores - 1))
            elif worst >= cfg.thermal_warm_pct or load1 >= cores * 0.9:
                pressure, budget = "warm", max(2, min(3, cores))
            else:
                pressure, budget = "ok", max(2, cores)

        return _ThermalSnap(
            ts=time.time(),
            cpu_pct=round(worst, 1),
            mem_pct=round(mem_pct, 1),
            swap_pct=round(swap_pct, 1),
            net_recv_mb=round(net_recv, 2),
            net_sent_mb=round(net_sent, 2),
            load1=round(load1, 2),
            threads=n_threads,
            pressure=pressure,
            budget=budget,
        )

    async def _loop(self) -> None:
        cfg = _cfg()
        interval = max(0.5, cfg.thermal_interval_s)
        if _HAS_PSUTIL:
            with contextlib.suppress(Exception):
                psutil.cpu_percent(interval=None)
        while True:
            try:
                await asyncio.sleep(interval)
                snap = self._compute(interval)
                with self._lock:
                    prev = self._snap.pressure
                    self._snap = snap
                if snap.pressure != prev:
                    logger.info(
                        "thermal_pressure_change %s->%s cpu=%.1f load1=%.2f budget=%d",
                        prev,
                        snap.pressure,
                        snap.cpu_pct,
                        snap.load1,
                        snap.budget,
                    )
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.debug("thermal_loop_error error=%s", e)

    def start(self) -> bool:
        if self._running:
            return True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        self._task = loop.create_task(self._loop(), name="thermal_guard")
        self._running = True
        logger.info("thermal_guard_started")
        return True

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
        self._running = False

    def running(self) -> bool:
        return self._running

    def budget(self) -> int:
        with self._lock:
            return self._snap.budget

    def pressure(self) -> str:
        with self._lock:
            return self._snap.pressure

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            s = self._snap
        return {
            "ts": s.ts,
            "cpu_pct": s.cpu_pct,
            "mem_pct": s.mem_pct,
            "swap_pct": s.swap_pct,
            "net_recv_mb": s.net_recv_mb,
            "net_sent_mb": s.net_sent_mb,
            "load1": s.load1,
            "threads": s.threads,
            "pressure": s.pressure,
            "budget": s.budget,
            "running": self._running,
            "psutil": _HAS_PSUTIL,
        }


_GUARD = _ThermalGuard()


def start() -> bool:
    return _GUARD.start()


def stop() -> None:
    _GUARD.stop()


def running() -> bool:
    return _GUARD.running()


def budget() -> int:
    return _GUARD.budget()


def pressure() -> str:
    return _GUARD.pressure()


def snapshot() -> dict[str, Any]:
    return _GUARD.snapshot()


# ═══════════════════════════════════════════════════════════════════════════
# §2 — Micro-volatile mode
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(slots=True)
class _MicroState:
    active: bool = False
    reason: str = ""
    entered_at: float = 0.0
    ceiling_mb: int = 15
    entries: int = 0
    exits: int = 0


_MICRO_LOCK = threading.RLock()
_MICRO = _MicroState()


def enter(reason: str = "manual", *, ceiling_mb: int | None = None) -> None:
    cfg = _cfg()
    with _MICRO_LOCK:
        if _MICRO.active:
            return
        _MICRO.active = True
        _MICRO.reason = (reason or "")[:120]
        _MICRO.entered_at = time.time()
        _MICRO.ceiling_mb = int(ceiling_mb if ceiling_mb is not None else cfg.micro_volatile_mb)
        _MICRO.entries += 1
    logger.warning("micro_volatile_entered reason=%s ceiling_mb=%d", reason, _MICRO.ceiling_mb)


def exit() -> None:
    with _MICRO_LOCK:
        if not _MICRO.active:
            return
        _MICRO.active = False
        _MICRO.exits += 1
        held_s = time.time() - _MICRO.entered_at
    logger.info("micro_volatile_exited held_s=%.1f", held_s)


def is_active() -> bool:
    with _MICRO_LOCK:
        return _MICRO.active


def ceiling_mb() -> int:
    with _MICRO_LOCK:
        return _MICRO.ceiling_mb


def clamp_max_tokens(requested: int) -> int:
    if not is_active():
        return requested
    return min(int(requested), _cfg().micro_volatile_tokens)


def clamp_response(text: str) -> str:
    if not is_active() or not isinstance(text, str):
        return text
    cap = _cfg().micro_volatile_chars
    if len(text) <= cap:
        return text
    return text[:cap].rstrip() + "\n…[truncated: micro-volatile mode]"


def policy() -> dict[str, Any]:
    cfg = _cfg()
    with _MICRO_LOCK:
        return {
            "active": _MICRO.active,
            "reason": _MICRO.reason,
            "ceiling_mb": _MICRO.ceiling_mb,
            "max_tokens": cfg.micro_volatile_tokens,
            "max_chars": cfg.micro_volatile_chars,
            "held_s": round(time.time() - _MICRO.entered_at, 1) if _MICRO.active else 0.0,
        }


# ═══════════════════════════════════════════════════════════════════════════
# §3 — Override advisory
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(slots=True)
class _Decision:
    decision_id: str
    session_id: str
    ram_free_mb: int
    seconds: int
    issued_at: float
    proceed: bool | None = None
    decided_at: float = 0.0
    correlated_volatile_entry: bool = False


_PENDING: dict[str, _Decision] = {}
_MAX_PENDING = 512
_GC_INTERVAL_S = 60.0
_STOP_GC = threading.Event()


def _gc_locked() -> None:
    cfg = _cfg()
    now = time.time()
    stale = [k for k, d in _PENDING.items() if (now - d.issued_at) > cfg.override_ttl_s]
    for k in stale:
        _PENDING.pop(k, None)
    if len(_PENDING) > cfg.override_max_pending:
        ordered = sorted(_PENDING.items(), key=lambda kv: kv[1].issued_at)
        for k, _ in ordered[: len(_PENDING) - cfg.override_max_pending]:
            _PENDING.pop(k, None)


def _gc_worker() -> None:
    while not _STOP_GC.wait(_GC_INTERVAL_S):
        with _MICRO_LOCK:
            _gc_locked()


threading.Thread(target=_gc_worker, daemon=True, name="guards-override-gc").start()


def issue(ram_free_mb: int, session_id: str = "", *, seconds: int | None = None) -> dict[str, Any]:
    cfg = _cfg()
    secs = int(seconds if seconds is not None else cfg.override_seconds)
    secs = max(1, min(secs, 30))
    did = uuid.uuid4().hex[:16]
    with _MICRO_LOCK:
        _gc_locked()
        _PENDING[did] = _Decision(
            decision_id=did, session_id=session_id, ram_free_mb=int(ram_free_mb), seconds=secs, issued_at=time.time()
        )
    logger.info("advisory_issued decision_id=%s ram_free_mb=%d seconds=%d", did, ram_free_mb, secs)
    return {
        "decision_id": did,
        "kind": "system_resource_advisory",
        "severity": "warning",
        "ram_free_mb": int(ram_free_mb),
        "countdown_seconds": secs,
        "title": "System Resource Advisory",
        "message": (
            f"Device reports only {ram_free_mb} MB free RAM. "
            f"Proceeding will run in a restricted 15 MB "
            f"micro-volatile envelope. Performance may degrade "
            f"and the browser thread could stutter. Proceed at "
            f"your own risk."
        ),
        "actions": [
            {"id": "abort", "label": "Cancel", "primary": False},
            {"id": "proceed", "label": f"Proceed anyway ({secs}s)", "primary": True},
        ],
        "auto_proceed_on_timeout": False,
        "issued_at": time.time(),
    }


def record_decision(decision_id: str, proceed: bool) -> None:
    with _MICRO_LOCK:
        d = _PENDING.get(decision_id)
        if d is None:
            return
        d.proceed = bool(proceed)
        d.decided_at = time.time()
        if proceed and not _MICRO.active:
            enter(reason=f"user_override_{decision_id}")
            d.correlated_volatile_entry = True
            logger.info("cross_guard_activated decision_id=%s", decision_id)


def should_proceed(decision_id: str) -> bool:
    with _MICRO_LOCK:
        d = _PENDING.get(decision_id)
        if d is None or d.proceed is None:
            return False
        return d.proceed


# ═══════════════════════════════════════════════════════════════════════════
# §4 — Burst scheduler
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(slots=True)
class BurstToken:
    name: str
    started_at: float
    deadline_at: float
    token_id: str
    _cancelled: bool = False
    _task: asyncio.Task | None = None
    _on_expire: Callable[[str], None] | None = None

    def expired(self) -> bool:
        return time.monotonic() >= self.deadline_at

    def remaining_s(self) -> float:
        return max(0.0, self.deadline_at - time.monotonic())

    def cancel(self) -> None:
        self._cancelled = True

    def is_cancelled(self) -> bool:
        return self._cancelled

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "token_id": self.token_id,
            "age_s": round(time.monotonic() - self.started_at, 1),
            "remaining_s": round(self.remaining_s(), 1),
            "deadline_in_s": round(self.deadline_at - self.started_at, 1),
            "cancelled": self._cancelled,
        }


class _BurstScheduler:
    __slots__ = ("_lock", "_active", "_counter", "_stats")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, BurstToken] = {}
        self._counter = 0
        self._stats = {"acquired": 0, "released": 0, "expired": 0, "rejected_cap": 0}

    def acquire(
        self, name: str, *, minutes: float | None = None, on_expire: Callable[[str], None] | None = None
    ) -> BurstToken | None:
        cfg = _cfg()
        requested = minutes if minutes is not None else cfg.burst_default_min
        requested = max(0.5, min(float(requested), cfg.burst_hard_max_min))
        with self._lock:
            if len(self._active) >= cfg.burst_max_active:
                self._stats["rejected_cap"] += 1
                logger.warning("burst_rejected name=%s cap=%d", name, cfg.burst_max_active)
                return None
            self._counter += 1
            now = time.monotonic()
            tok = BurstToken(
                name=name,
                started_at=now,
                deadline_at=now + requested * 60.0,
                token_id=f"{name}::{self._counter}",
                _on_expire=on_expire,
            )
            self._active[tok.token_id] = tok
            self._stats["acquired"] += 1
        try:
            loop = asyncio.get_running_loop()
            tok._task = loop.create_task(self._expiry_watch(tok), name=f"burst::{tok.token_id}")
        except RuntimeError:
            pass
        return tok

    async def _expiry_watch(self, tok: BurstToken) -> None:
        try:
            while True:
                remaining = tok.remaining_s()
                if remaining <= 0:
                    break
                if tok._cancelled:
                    return
                await asyncio.sleep(min(1.0, remaining))
            if tok._cancelled:
                return
            logger.warning("burst_expired name=%s token=%s", tok.name, tok.token_id)
            with self._lock:
                self._stats["expired"] += 1
            if tok._on_expire is not None:
                with contextlib.suppress(Exception):
                    tok._on_expire(tok.token_id)
            self.release(tok)
        except asyncio.CancelledError:
            return

    def release(self, tok: BurstToken | None) -> None:
        if tok is None:
            return
        with self._lock:
            removed = self._active.pop(tok.token_id, None)
            if removed is not None:
                self._stats["released"] += 1
        if tok._task is not None and not tok._task.done():
            tok._task.cancel()
            tok._task = None

    def active(self) -> list[dict[str, Any]]:
        with self._lock:
            return [t.to_dict() for t in self._active.values()]

    def stats(self) -> dict[str, Any]:
        cfg = _cfg()
        with self._lock:
            return {
                "active": len(self._active),
                "max_active": cfg.burst_max_active,
                "default_min": cfg.burst_default_min,
                "hard_max_min": cfg.burst_hard_max_min,
                **dict(self._stats),
            }

    def shutdown(self) -> None:
        with self._lock:
            tokens = list(self._active.values())
            self._active.clear()
        for t in tokens:
            if t._task is not None and not t._task.done():
                t._task.cancel()


_BURST = _BurstScheduler()


def burst_acquire(
    name: str, *, minutes: float | None = None, on_expire: Callable[[str], None] | None = None
) -> BurstToken | None:
    return _BURST.acquire(name, minutes=minutes, on_expire=on_expire)


def burst_release(tok: BurstToken | None) -> None:
    _BURST.release(tok)


def burst_active() -> list[dict[str, Any]]:
    return _BURST.active()


def burst_stats() -> dict[str, Any]:
    return _BURST.stats()


def burst_shutdown() -> None:
    _BURST.shutdown()


# ═══════════════════════════════════════════════════════════════════════════
# §5 — Token governor
# ═══════════════════════════════════════════════════════════════════════════
def _tier_limits(tier: str) -> tuple[int, int]:
    cfg = _cfg()
    return {
        "free": (cfg.tok_free_min, cfg.tok_free_day),
        "pro": (cfg.tok_pro_min, cfg.tok_pro_day),
        "business": (cfg.tok_biz_min, cfg.tok_biz_day),
        "guest": (cfg.tok_gst_min, cfg.tok_gst_day),
    }.get(tier or "free", (cfg.tok_free_min, cfg.tok_free_day))


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


@dataclass(slots=True)
class _Bucket:
    minute: deque[tuple[float, int]] = field(default_factory=deque)
    day: deque[tuple[float, int]] = field(default_factory=deque)


_MEM: "OrderedDict[str, _Bucket]" = OrderedDict()
_MEM_LOCK = asyncio.Lock()
_MINUTE_S = 60
_DAY_S = 86_400


def _prune_bucket(b: _Bucket, now: float) -> None:
    cutoff_min = now - _MINUTE_S
    while b.minute and b.minute[0][0] < cutoff_min:
        b.minute.popleft()
    cutoff_day = now - _DAY_S
    while b.day and b.day[0][0] < cutoff_day:
        b.day.popleft()


def _sum_bucket(b: _Bucket) -> tuple[int, int]:
    return (sum(t for _, t in b.minute), sum(t for _, t in b.day))


def _get_bucket_lru(uid: str) -> _Bucket:
    b = _MEM.get(uid)
    if b is not None:
        _MEM.move_to_end(uid)
        return b
    b = _Bucket()
    _MEM[uid] = b
    while len(_MEM) > _cfg().tok_max_users:
        _MEM.popitem(last=False)
    return b


def _mem_check(uid: str, tier: str, est: int) -> tuple[bool, int, int]:
    now = time.time()
    b = _get_bucket_lru(uid)
    _prune_bucket(b, now)
    used_min, used_day = _sum_bucket(b)
    cap_min, cap_day = _tier_limits(tier)
    if used_min + est > cap_min:
        retry = max(1, int(b.minute[0][0] + _MINUTE_S - now)) if b.minute else 1
        return False, max(0, cap_min - used_min), retry
    if used_day + est > cap_day:
        retry = max(1, int(b.day[0][0] + _DAY_S - now)) if b.day else 1
        return False, max(0, cap_day - used_day), retry
    b.minute.append((now, est))
    b.day.append((now, est))
    return True, cap_min - (used_min + est), 0


async def _redis_check(uid: str, tier: str, est: int) -> tuple[bool, int, int] | None:
    try:
        from .infra import get_redis_client

        r = get_redis_client()
    except Exception:
        return None
    if r is None:
        return None
    try:
        cap_min, cap_day = _tier_limits(tier)
        now = int(time.time())
        kmin = f"tok:{uid}:m:{now // _MINUTE_S}"
        kday = f"tok:{uid}:d:{now // _DAY_S}"
        async with r.pipeline(transaction=True) as pipe:
            pipe.incrby(kmin, est)
            pipe.expire(kmin, _MINUTE_S + 5)
            pipe.incrby(kday, est)
            pipe.expire(kday, _DAY_S + 5)
            res = await pipe.execute()
        used_min, used_day = int(res[0]), int(res[2])
        if used_min > cap_min:
            return False, max(0, cap_min - (used_min - est)), _MINUTE_S - (now % _MINUTE_S)
        if used_day > cap_day:
            return False, max(0, cap_day - (used_day - est)), _DAY_S - (now % _DAY_S)
        return True, cap_min - used_min, 0
    except Exception as e:
        logger.debug("governor_redis_error error=%s", e)
        return None


async def reserve(uid: str, tier: str, est_tokens: int) -> tuple[bool, int, int]:
    est = max(0, int(est_tokens))
    if est == 0 or not uid:
        return True, 0, 0
    outcome = await _redis_check(uid, tier, est)
    if outcome is not None:
        return outcome
    async with _MEM_LOCK:
        return _mem_check(uid, tier, est)


async def settle(uid: str, delta_tokens: int) -> None:
    if not uid or delta_tokens == 0:
        return
    try:
        from .infra import get_redis_client

        r = get_redis_client()
        if r is not None:
            now = int(time.time())
            await r.incrby(f"tok:{uid}:m:{now // _MINUTE_S}", int(delta_tokens))
            await r.incrby(f"tok:{uid}:d:{now // _DAY_S}", int(delta_tokens))
            return
    except Exception:
        pass
    async with _MEM_LOCK:
        b = _MEM.get(uid)
        if b is not None:
            _MEM.move_to_end(uid)
            b.minute.append((time.time(), int(delta_tokens)))
            b.day.append((time.time(), int(delta_tokens)))


async def snapshot_usage(uid: str, tier: str) -> dict[str, Any]:
    cap_min, cap_day = _tier_limits(tier)
    try:
        from .infra import get_redis_client

        r = get_redis_client()
        if r is not None:
            now = int(time.time())
            um = await r.get(f"tok:{uid}:m:{now // _MINUTE_S}") or 0
            ud = await r.get(f"tok:{uid}:d:{now // _DAY_S}") or 0
            return {
                "tier": tier,
                "min_used": int(um),
                "min_cap": cap_min,
                "day_used": int(ud),
                "day_cap": cap_day,
                "min_remaining": max(0, cap_min - int(um)),
                "day_remaining": max(0, cap_day - int(ud)),
            }
    except Exception:
        pass
    async with _MEM_LOCK:
        b = _get_bucket_lru(uid)
        _prune_bucket(b, time.time())
        um, ud = _sum_bucket(b)
        return {
            "tier": tier,
            "min_used": um,
            "min_cap": cap_min,
            "day_used": ud,
            "day_cap": cap_day,
            "min_remaining": max(0, cap_min - um),
            "day_remaining": max(0, cap_day - ud),
        }


# ═══════════════════════════════════════════════════════════════════════════
# Audit
# ═══════════════════════════════════════════════════════════════════════════
def audit() -> dict[str, Any]:
    with _MICRO_LOCK:
        return {
            "thermal": snapshot(),
            "micro_volatile": {"active": _MICRO.active, "entries": _MICRO.entries, "exits": _MICRO.exits},
            "override": {"pending": len(_PENDING)},
            "burst": burst_stats(),
            "governor": {"users_tracked": len(_MEM), "max_users": _cfg().tok_max_users},
        }


__all__ = [
    # Thermal
    "start",
    "stop",
    "running",
    "budget",
    "pressure",
    "snapshot",
    # Micro-volatile
    "enter",
    "exit",
    "is_active",
    "ceiling_mb",
    "clamp_max_tokens",
    "clamp_response",
    "policy",
    # Override
    "issue",
    "record_decision",
    "should_proceed",
    # Burst
    "BurstToken",
    "burst_acquire",
    "burst_release",
    "burst_active",
    "burst_stats",
    "burst_shutdown",
    # Governor
    "estimate_tokens",
    "reserve",
    "settle",
    "snapshot_usage",
    # Audit
    "audit",
]
