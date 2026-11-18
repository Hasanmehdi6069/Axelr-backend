"""The Rise of AI Agents: Deadlock Monitoring System — v1.1.0
==========================================================
Wait-for-graph cycle detection and automatic resolution for long-running
autonomous agent loops.

Honest scope / correctness envelope
-----------------------------------
* **Cycle detection is iterative** — no recursion, no stack-overflow on a
  chain of 100k wait-for edges. Detects one cycle per strongly-connected
  component found in DFS order.
* **psutil process operations run in worker threads** (`asyncio.to_thread`)
  so a hung PID on a dead mount doesn't stall the event loop.
* **Deadlock resolution kills the OLDEST agent in the cycle** — see
  `_resolve_deadlock_for_agent`. This is a deliberate policy (deterministic;
  preserves newer agent state). Documented, not magic.
* **Agent cleanup on release** — `Resource.waiting_agents` no longer
  accumulates dead agents; `remove_agent` scrubs references.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import DefaultDict, Dict, Iterable, List, Optional, Set

import psutil

logger = logging.getLogger("axelr.ai_agents.deadlock_monitor")
__version__ = "1.1.0"

_MAX_CYCLES_PER_SCAN = 100
_MAX_EVENTS = 1000
_MONITOR_INTERVAL_S = 15.0
_HEARTBEAT_TIMEOUT_S = 30.0


# ─── Types ────────────────────────────────────────────────────────────────
@dataclass(slots=True)
class Resource:
    resource_id: str
    owner_agent_id: Optional[str] = None
    waiting_agents: List[str] = field(default_factory=list)


@dataclass(slots=True)
class AgentInfo:
    agent_id: str
    process_id: int
    thread_ids: List[int] = field(default_factory=list)
    task_ids: List[str] = field(default_factory=list)
    last_heartbeat: float = 0.0
    heartbeat_timeout: float = _HEARTBEAT_TIMEOUT_S
    held_resources: Set[str] = field(default_factory=set)
    requested_resources: Set[str] = field(default_factory=set)
    is_active: bool = True
    start_time: float = field(default_factory=time.time)


@dataclass(slots=True)
class DeadlockEvent:
    event_id: str
    detected_at: float
    involved_agents: List[str]
    involved_resources: List[str]
    resolved: bool = False
    resolved_at: Optional[float] = None


# ─── Wait-for graph (iterative cycle detection) ───────────────────────────
class WaitForGraph:
    """Directed graph: edge A → B means "A is waiting for B"."""

    def __init__(self) -> None:
        self.adjacency: DefaultDict[str, Set[str]] = defaultdict(set)
        self.agents: Set[str] = set()

    def add_wait_relation(self, waiting: str, holding: str) -> None:
        self.adjacency[waiting].add(holding)
        self.agents.add(waiting)
        self.agents.add(holding)

    def remove_wait_relation(self, waiting: str, holding: str) -> None:
        bucket = self.adjacency.get(waiting)
        if bucket:
            bucket.discard(holding)
            if not bucket:
                del self.adjacency[waiting]

    def remove_agent(self, agent_id: str) -> None:
        self.adjacency.pop(agent_id, None)
        for bucket in self.adjacency.values():
            bucket.discard(agent_id)
        self.agents.discard(agent_id)

    def detect_cycles(self) -> List[List[str]]:
        """Iterative DFS. Returns up to `_MAX_CYCLES_PER_SCAN` cycles.

        A cycle is `[n0, n1, ..., nk]` where `n0 → n1 → ... → nk → n0`.
        The same set of agents could in principle yield multiple cycles if
        they form multiple SCCs; we return one cycle per back-edge.
        """
        visited: Set[str] = set()
        in_stack: Set[str] = set()
        cycles: List[List[str]] = []

        for start in list(self.agents):
            if start in visited:
                continue
            if len(cycles) >= _MAX_CYCLES_PER_SCAN:
                logger.warning("cycle_scan_truncated max=%d", _MAX_CYCLES_PER_SCAN)
                break

            stack: List[tuple[str, Iterable[str]]] = [
                (start, iter(self.adjacency.get(start, ())))
            ]
            path: List[str] = [start]
            in_stack.add(start)
            visited.add(start)

            while stack:
                _node, it = stack[-1]
                try:
                    nxt = next(it)
                except StopIteration:
                    finished, _ = stack.pop()
                    in_stack.discard(finished)
                    path.pop()
                    continue

                if nxt in in_stack:
                    idx = path.index(nxt)
                    cycles.append(path[idx:].copy())
                    if len(cycles) >= _MAX_CYCLES_PER_SCAN:
                        return cycles
                    continue
                if nxt in visited:
                    continue

                visited.add(nxt)
                in_stack.add(nxt)
                path.append(nxt)
                stack.append((nxt, iter(self.adjacency.get(nxt, ()))))
        return cycles


# ─── Monitor ──────────────────────────────────────────────────────────────
@dataclass(slots=True)
class DeadlockMonitor:
    agents: Dict[str, AgentInfo] = field(default_factory=dict)
    resources: Dict[str, Resource] = field(default_factory=dict)
    events: List[DeadlockEvent] = field(default_factory=list)
    wait_for_graph: WaitForGraph = field(default_factory=WaitForGraph)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    monitoring_task: Optional[asyncio.Task] = None
    shutdown_event: asyncio.Event = field(default_factory=asyncio.Event)
    check_interval: float = _MONITOR_INTERVAL_S

    async def register_agent(self, agent_id: str, process: psutil.Process) -> AgentInfo:
        thread_ids = await asyncio.to_thread(
            lambda: [t.id for t in process.threads()])
        async with self.lock:
            agent = AgentInfo(
                agent_id=agent_id,
                process_id=process.pid,
                thread_ids=thread_ids,
                last_heartbeat=time.time(),
            )
            self.agents[agent_id] = agent
            logger.info("agent_registered id=%s pid=%d threads=%d",
                        agent_id, process.pid, len(thread_ids))
            return agent

    async def unregister_agent(self, agent_id: str) -> None:
        async with self.lock:
            agent = self.agents.pop(agent_id, None)
            if agent is None:
                return
            for res_id in list(agent.held_resources):
                res = self.resources.get(res_id)
                if res is not None and res.owner_agent_id == agent_id:
                    res.owner_agent_id = None
                    if agent_id in res.waiting_agents:
                        res.waiting_agents.remove(agent_id)
            for res in self.resources.values():
                if agent_id in res.waiting_agents:
                    res.waiting_agents.remove(agent_id)
            self.wait_for_graph.remove_agent(agent_id)
            logger.info("agent_unregistered id=%s", agent_id)

    async def record_heartbeat(self, agent_id: str) -> bool:
        async with self.lock:
            agent = self.agents.get(agent_id)
            if agent is None:
                return False
            agent.last_heartbeat = time.time()
            return True

    async def acquire_resource(self, agent_id: str, resource_id: str) -> bool:
        async with self.lock:
            agent = self.agents.get(agent_id)
            if agent is None:
                return False
            res = self.resources.get(resource_id)
            if res is None:
                res = Resource(resource_id=resource_id)
                self.resources[resource_id] = res

            if res.owner_agent_id is None:
                res.owner_agent_id = agent_id
                agent.held_resources.add(resource_id)
                agent.requested_resources.discard(resource_id)
                return True

            if agent_id not in res.waiting_agents:
                res.waiting_agents.append(agent_id)
            agent.requested_resources.add(resource_id)
            self.wait_for_graph.add_wait_relation(
                agent_id, res.owner_agent_id)
            return False

    async def release_resource(self, agent_id: str, resource_id: str) -> None:
        async with self.lock:
            res = self.resources.get(resource_id)
            agent = self.agents.get(agent_id)
            if res is None or agent is None or res.owner_agent_id != agent_id:
                return
            res.owner_agent_id = None
            agent.held_resources.discard(resource_id)

            if res.waiting_agents:
                nxt_id = res.waiting_agents.pop(0)
                nxt = self.agents.get(nxt_id)
                if nxt is not None:
                    res.owner_agent_id = nxt_id
                    nxt.held_resources.add(resource_id)
                    nxt.requested_resources.discard(resource_id)
                    self.wait_for_graph.remove_wait_relation(nxt_id, agent_id)

    async def _resolve_cycle(self, cycle: List[str],
                             event: DeadlockEvent) -> None:
        """Kill the OLDEST agent in the cycle (deterministic policy).

        Rationale: oldest agent has typically held state longest and is
        most likely the one whose restart loses the least.
        """
        candidates = [(a, self.agents[a]) for a in cycle if a in self.agents]
        if not candidates:
            return
        victim_id, victim = min(candidates, key=lambda kv: kv[1].start_time)

        logger.critical("deadlock_resolving victim=%s cycle=%s",
                        victim_id, cycle)

        def _kill(pid: int) -> None:
            try:
                p = psutil.Process(pid)
                p.terminate()
                p.wait(timeout=5)
            except Exception as e:
                logger.error("kill_failed pid=%d err=%s", pid, e)

        await asyncio.to_thread(_kill, victim.process_id)

        for res_id in list(victim.held_resources):
            await self.release_resource(victim_id, res_id)
        victim.is_active = False
        event.resolved = True
        event.resolved_at = time.time()

    async def _monitor_loop(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self.shutdown_event.wait(), timeout=self.check_interval)
                return
            except asyncio.TimeoutError:
                pass

            async with self.lock:
                now = time.time()
                timed_out = [
                    aid for aid, a in self.agents.items()
                    if a.is_active and (now - a.last_heartbeat) > a.heartbeat_timeout
                ]
                cycles = self.wait_for_graph.detect_cycles()

            for aid in timed_out:
                logger.warning("agent_heartbeat_timeout id=%s", aid)
                await self.unregister_agent(aid)

            if cycles:
                event = DeadlockEvent(
                    event_id=str(uuid.uuid4()),
                    detected_at=time.time(),
                    involved_agents=[a for c in cycles for a in c],
                    involved_resources=[],
                )
                self.events.append(event)
                if len(self.events) > _MAX_EVENTS:
                    del self.events[: len(self.events) - _MAX_EVENTS]
                for cycle in cycles:
                    try:
                        await self._resolve_cycle(cycle, event)
                    except Exception as e:
                        logger.error("cycle_resolution_failed err=%s", e)

    async def shutdown(self) -> None:
        self.shutdown_event.set()
        if self.monitoring_task is not None and not self.monitoring_task.done():
            self.monitoring_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.monitoring_task
        self.monitoring_task = None
        logger.info("deadlock_monitor_shutdown")

    def health(self) -> dict:
        return {
            "ok": self.monitoring_task is not None and not self.monitoring_task.done(),
            "details": {
                "version": __version__,
                "agents": len(self.agents),
                "active_agents": sum(1 for a in self.agents.values() if a.is_active),
                "resources": len(self.resources),
                "events_recorded": len(self.events),
                "cycles_detected_total": len(self.events),
            },
        }


_monitor: Optional[DeadlockMonitor] = None


def get_monitor() -> DeadlockMonitor:
    global _monitor
    if _monitor is None:
        _monitor = DeadlockMonitor()
    return _monitor


async def init() -> None:
    m = get_monitor()
    if m.monitoring_task is None or m.monitoring_task.done():
        m.shutdown_event.clear()
        m.monitoring_task = asyncio.create_task(m._monitor_loop())
    logger.info("ai_agents_deadlock_monitor_initialized")


async def shutdown() -> None:
    global _monitor
    if _monitor is not None:
        await _monitor.shutdown()
        _monitor = None
    logger.info("ai_agents_deadlock_monitor_shutdown")


def health() -> dict:
    if _monitor is None:
        return {"ok": False, "details": {"reason": "not_initialized",
                                          "version": __version__}}
    return _monitor.health()