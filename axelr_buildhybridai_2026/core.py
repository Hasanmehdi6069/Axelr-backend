"""#BuildHybridAI 2026 Edge-Cloud Dual Stream Sync — v1.1.0
=======================================================
Intelligent workload placement between edge and cloud.

Honest scope
------------
* **Execution is delegated to injected callables** — `local_executor` and
  `cloud_executor`. This module does not fake execution with `sleep()`.
  If an executor is not injected, `submit_task` completes with a
  `NotImplementedError` on the task's `error` field.
* **`CRDTState` replaced with `LWWState`** — the old "vector clock" merge
  was really last-write-wins with silent data loss on ties. Renamed and
  fixed to use `(timestamp, node_id)` tiebreak.
* **Persistent `aiohttp.ClientSession`** — the old loops created a new
  session per iteration, leaking file descriptors.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

import aiohttp

logger = logging.getLogger("axelr.buildhybridai.core")
__version__ = "1.1.0"

_MAX_PENDING = 200
_MAX_COMPLETED = 1000


@dataclass(slots=True)
class NetworkConditions:
    latency_ms: float
    bandwidth_mbps: float
    packet_loss_rate: float
    is_metered: bool
    battery_level: Optional[float] = None
    timestamp: float = field(default_factory=time.time)


@dataclass(slots=True)
class ComputationTask:
    task_id: str
    task_type: str
    compute_intensity: float
    data_size_mb: float
    result_size_mb: float
    required_gpu: bool = False
    min_bandwidth_mbps: float = 1.0
    max_acceptable_latency_ms: float = 500.0
    assigned_location: Optional[str] = None
    status: str = "pending"
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None


@dataclass(slots=True)
class LWWEntry:
    value: Any
    timestamp: float
    node_id: str


@dataclass(slots=True)
class LWWState:
    """Last-write-wins register per key. Ties broken by `node_id`
    lexicographic order — deterministic, no silent loss."""
    state: Dict[str, LWWEntry] = field(default_factory=dict)
    last_synced: float = 0.0

    def set(self, key: str, value: Any, node_id: str = "local") -> None:
        self.state[key] = LWWEntry(value=value, timestamp=time.time(),
                                   node_id=node_id)

    def merge(self, other: "LWWState") -> None:
        for k, e in other.state.items():
            existing = self.state.get(k)
            if (existing is None
                    or (e.timestamp, e.node_id) > (existing.timestamp, existing.node_id)):
                self.state[k] = e

    def snapshot(self) -> Dict[str, Any]:
        return {k: v.value for k, v in self.state.items()}


class WorkloadPlacer:
    def decide_placement(
        self, task: ComputationTask, network: NetworkConditions,
        has_local_gpu: bool,
    ) -> str:
        if task.required_gpu and not has_local_gpu:
            return "cloud"
        if network.bandwidth_mbps < task.min_bandwidth_mbps:
            return "edge"
        if (network.latency_ms > task.max_acceptable_latency_ms
                and task.compute_intensity < 0.5):
            return "edge"
        if task.compute_intensity > 0.7 and network.latency_ms < 200:
            return "cloud"
        return "edge"


LocalExecutor = Callable[[ComputationTask], Awaitable[Dict[str, Any]]]
CloudExecutor = Callable[[ComputationTask, aiohttp.ClientSession],
                         Awaitable[Dict[str, Any]]]


@dataclass(slots=True)
class HybridOrchestrator:
    placer: WorkloadPlacer = field(default_factory=WorkloadPlacer)
    local_state: LWWState = field(default_factory=LWWState)
    cloud_state: LWWState = field(default_factory=LWWState)
    pending_tasks: Optional[asyncio.Queue] = None
    completed_tasks: Dict[str, ComputationTask] = field(default_factory=dict)
    network_monitor_task: Optional[asyncio.Task] = None
    sync_task: Optional[asyncio.Task] = None
    worker_tasks: List[asyncio.Task] = field(default_factory=list)
    current_network: NetworkConditions = field(default_factory=lambda: NetworkConditions(
        latency_ms=50.0, bandwidth_mbps=100.0,
        packet_loss_rate=0.0, is_metered=False,
    ))
    cloud_endpoint: Optional[str] = None
    has_local_gpu: bool = False
    is_running: bool = False
    max_concurrent_tasks: int = 8
    local_executor: Optional[LocalExecutor] = None
    cloud_executor: Optional[CloudExecutor] = None
    shutdown_event: asyncio.Event = field(default_factory=asyncio.Event)
    _session: Optional[aiohttp.ClientSession] = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @classmethod
    async def create(
        cls, cloud_endpoint: Optional[str] = None,
        local_executor: Optional[LocalExecutor] = None,
        cloud_executor: Optional[CloudExecutor] = None,
    ) -> "HybridOrchestrator":
        has_gpu = await asyncio.to_thread(_torch_cuda_available)
        o = cls(cloud_endpoint=cloud_endpoint,
                has_local_gpu=has_gpu,
                local_executor=local_executor,
                cloud_executor=cloud_executor)
        o.pending_tasks = asyncio.Queue(maxsize=_MAX_PENDING)
        o.is_running = True
        o.shutdown_event.clear()
        o.network_monitor_task = asyncio.create_task(o._network_loop())
        o.sync_task = asyncio.create_task(o._sync_loop())
        for i in range(o.max_concurrent_tasks):
            o.worker_tasks.append(asyncio.create_task(o._worker(i)))
        logger.info("hybrid_orchestrator_started cloud=%s gpu=%s",
                    cloud_endpoint or "disabled", has_gpu)
        return o

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15.0))
        return self._session

    async def _network_loop(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self.shutdown_event.wait(), timeout=10.0)
                return
            except asyncio.TimeoutError:
                pass
            if not self.cloud_endpoint:
                continue
            try:
                t0 = time.time()
                async with self._get_session().get(
                    f"{self.cloud_endpoint}/health",
                ) as resp:
                    if resp.status == 200:
                        self.current_network.latency_ms = (time.time() - t0) * 1000
            except Exception as e:
                logger.debug("network_probe_failed err=%s", e)
                self.current_network.latency_ms *= 1.5
                self.current_network.bandwidth_mbps *= 0.8

    async def _sync_loop(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self.shutdown_event.wait(), timeout=30.0)
                return
            except asyncio.TimeoutError:
                pass
            if not self.cloud_endpoint:
                continue
            try:
                self.cloud_state.merge(self.local_state)
                async with self._get_session().post(
                    f"{self.cloud_endpoint}/sync",
                    json={"state": self.cloud_state.snapshot()},
                ) as resp:
                    if resp.status == 200:
                        self.local_state.last_synced = time.time()
            except Exception as e:
                logger.debug("state_sync_failed err=%s", e)

    async def _worker(self, worker_id: int) -> None:
        while not self.shutdown_event.is_set():
            assert self.pending_tasks is not None
            try:
                task = await asyncio.wait_for(
                    self.pending_tasks.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            task.assigned_location = self.placer.decide_placement(
                task, self.current_network, self.has_local_gpu)
            task.started_at = time.time()
            task.status = "running"
            try:
                if task.assigned_location == "edge":
                    if self.local_executor is None:
                        raise NotImplementedError(
                            "requires backend: local_executor")
                    result = await self.local_executor(task)
                else:
                    if self.cloud_executor is None:
                        raise NotImplementedError(
                            "requires backend: cloud_executor")
                    result = await self.cloud_executor(task, self._get_session())
                task.result = result
                task.status = "completed"
            except Exception as e:
                task.status = "failed"
                task.error = str(e)[:300]
                logger.error("task_failed id=%s loc=%s err=%s",
                             task.task_id, task.assigned_location, e)
            finally:
                task.completed_at = time.time()
                async with self._lock:
                    self.completed_tasks[task.task_id] = task
                    if len(self.completed_tasks) > _MAX_COMPLETED:
                        oldest = sorted(
                            self.completed_tasks,
                            key=lambda k: self.completed_tasks[k].completed_at or 0,
                        )[0]
                        del self.completed_tasks[oldest]
                self.pending_tasks.task_done()

    async def submit_task(self, task: ComputationTask) -> str:
        assert self.pending_tasks is not None
        try:
            self.pending_tasks.put_nowait(task)
        except asyncio.QueueFull:
            raise RuntimeError("pending_queue_full — increase _MAX_PENDING")
        return task.task_id

    async def shutdown(self) -> None:
        self.is_running = False
        self.shutdown_event.set()
        for t in [self.network_monitor_task, self.sync_task, *self.worker_tasks]:
            if t is not None and not t.done():
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
        self.worker_tasks.clear()
        self.network_monitor_task = None
        self.sync_task = None
        if self._session is not None:
            with contextlib.suppress(Exception):
                await self._session.close()
            self._session = None
        async with self._lock:
            self.completed_tasks.clear()
        self.pending_tasks = None
        logger.info("hybrid_orchestrator_shutdown")


def _torch_cuda_available() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except Exception:
        return False


_orchestrator: Optional[HybridOrchestrator] = None


def get_orchestrator() -> Optional[HybridOrchestrator]:
    return _orchestrator


async def init(
    cloud_endpoint: Optional[str] = None,
    local_executor: Optional[LocalExecutor] = None,
    cloud_executor: Optional[CloudExecutor] = None,
) -> None:
    global _orchestrator
    if _orchestrator is not None:
        await _orchestrator.shutdown()
    _orchestrator = await HybridOrchestrator.create(
        cloud_endpoint=cloud_endpoint,
        local_executor=local_executor,
        cloud_executor=cloud_executor,
    )
    logger.info("buildhybridai_orchestrator_initialized")


async def shutdown() -> None:
    global _orchestrator
    if _orchestrator is not None:
        await _orchestrator.shutdown()
        _orchestrator = None
    logger.info("buildhybridai_orchestrator_shutdown")


def health() -> Dict[str, Any]:
    if _orchestrator is None:
        return {"ok": False, "details": {"reason": "not_initialized",
                                         "version": __version__}}
    return {
        "ok": _orchestrator.is_running,
        "details": {
            "version": __version__,
            "workers": len(_orchestrator.worker_tasks),
            "pending": _orchestrator.pending_tasks.qsize() if _orchestrator.pending_tasks else 0,
            "pending_max": _MAX_PENDING,
            "completed": len(_orchestrator.completed_tasks),
            "cloud_endpoint": _orchestrator.cloud_endpoint,
            "local_executor_configured": _orchestrator.local_executor is not None,
            "cloud_executor_configured": _orchestrator.cloud_executor is not None,
            "has_local_gpu": _orchestrator.has_local_gpu,
        },
    }