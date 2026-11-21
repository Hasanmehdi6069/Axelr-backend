# axelr_ipc.py
"""axelr_ipc.py — AsyncIPC WebSocket client + StressTester.

Split from utils.py. Matches protocol.json exactly:
  envelope {type, id, payload}
  handshake: first frame is {type:"init", ...}, reply is {type:"ready", ...}
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import tracemalloc
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

try:
    import aiohttp
except ImportError:
    aiohttp = None

from axelr_utils import IPCNotConnectedError, ProtocolError, get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# AsyncIPC — bidirectional WS client
# ═══════════════════════════════════════════════════════════════════════
@dataclass(frozen=True, slots=True)
class IPCConfig:
    url: str = "ws://127.0.0.1:8765/axelr/v1"
    reconnect_seconds: float = 2.0
    ping_seconds: float = 20.0
    request_timeout_s: float = 5.0
    handshake_timeout_s: float = 2.0


class AsyncIPC:
    """Bidirectional WebSocket client. Matches protocol.json exactly.

    Lifecycle:
        ipc = AsyncIPC(IPCConfig())
        await ipc.connect()
        result = await ipc.request("correct", {"word": "recieve"})
        ipc.on("transcript", handler)
        await ipc.close()
    """

    __slots__ = (
        "config",
        "_session",
        "_ws",
        "_listeners",
        "_pending",
        "_send_lock",
        "_running",
        "_recv_task",
        "_next_id",
        "_session_id",
    )

    def __init__(self, config: Optional[IPCConfig] = None) -> None:
        self.config = config or IPCConfig()
        self._session: Optional["aiohttp.ClientSession"] = None
        self._ws: Optional["aiohttp.ClientWebSocketResponse"] = None
        self._listeners: Dict[str, List[Callable[[Dict[str, Any]], None]]] = {}
        self._pending: Dict[int, asyncio.Future] = {}
        self._send_lock = asyncio.Lock()
        self._running = False
        self._recv_task: Optional[asyncio.Task] = None
        self._next_id = 1
        self._session_id: Optional[str] = None

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    def on(self, event: str, cb: Callable[[Dict[str, Any]], None]) -> None:
        self._listeners.setdefault(event, []).append(cb)

    def off(self, event: str, cb: Callable[[Dict[str, Any]], None]) -> None:
        lst = self._listeners.get(event)
        if lst and cb in lst:
            lst.remove(cb)

    async def connect(self) -> None:
        if aiohttp is None:
            raise RuntimeError("aiohttp not installed; AsyncIPC disabled")
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=3.0))
        self._ws = await self._session.ws_connect(self.config.url, heartbeat=self.config.ping_seconds)
        self._running = True
        self._recv_task = asyncio.create_task(self._recv_loop())
        try:
            await self._do_handshake()
        except Exception:
            await self.close()
            raise

    async def _do_handshake(self) -> None:
        req_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            async with self._send_lock:
                await self._ws.send_json(
                    {
                        "type": "init",
                        "id": req_id,
                        "payload": {"client_version": "1.0.0", "tier": "auto"},
                    }
                )
            env = await asyncio.wait_for(fut, timeout=self.config.handshake_timeout_s)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise ProtocolError("handshake timeout — no ready frame")
        if not env.get("ok", False):
            raise ProtocolError(f"handshake rejected: {env.get('payload')}")
        payload = env.get("payload") or {}
        self._session_id = payload.get("session_id")
        logger.info("ipc_connected session_id=%s tier=%s", self._session_id, payload.get("tier"))

    async def _recv_loop(self) -> None:
        try:
            async for msg in self._ws:
                if msg.type != 1:
                    continue
                try:
                    env = json.loads(msg.data)
                except json.JSONDecodeError:
                    logger.warning("ipc_invalid_json")
                    continue
                msg_id = env.get("id")
                if isinstance(msg_id, int) and msg_id in self._pending:
                    fut = self._pending.pop(msg_id)
                    if not fut.done():
                        fut.set_result(env)
                else:
                    evt = env.get("type", "")
                    for cb in self._listeners.get(evt, ()):
                        try:
                            cb(env.get("payload") or {})
                        except Exception:
                            logger.exception("ipc_listener_failed evt=%s", evt)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("ipc_recv_loop_died")
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(IPCNotConnectedError("connection closed"))
            self._pending.clear()

    async def request(self, type_: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self._running or self._ws is None or self._ws.closed:
            raise IPCNotConnectedError("call connect() first")
        req_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            async with self._send_lock:
                await self._ws.send_json({"type": type_, "id": req_id, "payload": payload})
            env = await asyncio.wait_for(fut, timeout=self.config.request_timeout_s)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise ProtocolError(f"request timeout: {type_} (id={req_id})")
        if not env.get("ok", True):
            err = env.get("payload") or {}
            raise ProtocolError(f"server error: {err.get('code')} — {err.get('message')}")
        return env.get("payload") or {}

    async def ping(self) -> None:
        await self.request("ping", {})

    async def close(self) -> None:
        self._running = False
        if self._recv_task is not None:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass
            self._recv_task = None
        if self._ws is not None and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None
        self._ws = None


# ═══════════════════════════════════════════════════════════════════════
# StressTester — tracemalloc-based leak detection
# ═══════════════════════════════════════════════════════════════════════
@dataclass(slots=True, frozen=True)
class StressTestResult:
    total_operations: int
    successful_operations: int
    failed_operations: int
    throughput_ops: float
    p50_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
    max_memory_mb: float
    memory_leak_detected: bool
    passed: bool
    failure_reasons: List[str]


class StressTester:
    """Thread-safe stress tester with tracemalloc-based leak detection."""

    SLO_THROUGHPUT_MIN = 500
    SLO_P95_LATENCY_MAX = 50.0
    SLO_ERROR_RATE_MAX = 0.001
    SLO_MEMORY_GROWTH_MAX = 0.10

    __slots__ = ("_logger", "_config", "_latencies", "_lock")

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self._logger = get_logger(__name__)
        self._config = config or {}
        self._latencies: List[float] = []
        self._lock = threading.Lock()

    @staticmethod
    def _percentile(sv: List[float], p: float) -> float:
        if not sv:
            return 0.0
        idx = max(0, min(len(sv) - 1, int(p * (len(sv) - 1))))
        return sv[idx]

    def record_operation(self, latency_seconds: float) -> None:
        with self._lock:
            self._latencies.append(latency_seconds)

    def run_stress_suite(
        self, target_fn: Callable[[], Any], num_operations: int = 10000, concurrency: int = 100
    ) -> StressTestResult:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with self._lock:
            self._latencies.clear()

        tracemalloc.start()
        baseline, _ = tracemalloc.get_traced_memory()

        start_time = time.perf_counter()
        successes = 0
        failures = 0
        failure_reasons: List[str] = []

        def _timed_call(fn):
            t0 = time.perf_counter()
            try:
                fn()
                return True
            except Exception:
                return False
            finally:
                latency = time.perf_counter() - t0
                self.record_operation(latency)

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [executor.submit(_timed_call, target_fn) for _ in range(num_operations)]
            for future in as_completed(futures):
                try:
                    if future.result():
                        successes += 1
                    else:
                        failures += 1
                except Exception as e:
                    failures += 1
                    if len(failure_reasons) < 10:
                        failure_reasons.append(str(e)[:200])

        end_time = time.perf_counter()
        current, _peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        total_time = max(1e-9, end_time - start_time)
        throughput = num_operations / total_time

        with self._lock:
            lat_sorted = sorted(self._latencies)

        p50 = self._percentile(lat_sorted, 0.50) * 1000
        p95 = self._percentile(lat_sorted, 0.95) * 1000
        p99 = self._percentile(lat_sorted, 0.99) * 1000

        if baseline < 1024 * 1024:
            memory_leak = False
        else:
            growth = (current - baseline) / max(1, baseline)
            memory_leak = growth > self.SLO_MEMORY_GROWTH_MAX

        error_rate = failures / num_operations if num_operations else 0.0
        passed = (
            throughput >= self.SLO_THROUGHPUT_MIN
            and p95 <= self.SLO_P95_LATENCY_MAX
            and error_rate <= self.SLO_ERROR_RATE_MAX
            and not memory_leak
        )

        result = StressTestResult(
            total_operations=num_operations,
            successful_operations=successes,
            failed_operations=failures,
            throughput_ops=throughput,
            p50_latency_ms=p50,
            p95_latency_ms=p95,
            p99_latency_ms=p99,
            max_memory_mb=current / (1024 * 1024),
            memory_leak_detected=memory_leak,
            passed=passed,
            failure_reasons=failure_reasons,
        )
        self._logger.info(
            "stress_test_complete passed=%d/%d throughput=%.0f/s p95=%.2fms", successes, num_operations, throughput, p95
        )
        return result


__all__ = [
    "IPCConfig",
    "AsyncIPC",
    "StressTestResult",
    "StressTester",
    "ProtocolError",
    "IPCNotConnectedError",
]
