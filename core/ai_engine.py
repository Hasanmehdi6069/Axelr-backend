# core/ai_engine.py
"""
AXELR Resilient AIRouter
========================
Lightweight provider-metrics helper with circuit-breaker cooldowns.

This module is STANDALONE — it performs no network I/O. The production
FastAPI app in `app.py` uses its own routing logic (route_ai_request_*)
and does not depend on this module's runtime behavior. It is provided so
external consumers can reuse the ranking + circuit-breaker primitives.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

__all__ = ["ProviderMetrics", "ResilientAIRouter"]


@dataclass
class ProviderMetrics:
    """Rolling success / latency metrics for a single AI provider."""

    name: str
    latencies: deque = field(default_factory=lambda: deque(maxlen=10))
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    cooldown_until: float = 0.0

    @property
    def is_available(self) -> bool:
        return time.time() >= self.cooldown_until

    @property
    def score(self) -> float:
        total = self.successes + self.failures
        rate = (self.successes / total) if total > 0 else 0.8
        avg_lat = (
            sum(self.latencies) / len(self.latencies) if self.latencies else 2.0
        )
        return (rate * 100) - (avg_lat * 15) - (self.consecutive_failures * 20)


class ResilientAIRouter:
    """
    Tracks provider health and returns a ranked provider list.

    Network invocation is the caller's responsibility — this class owns
    only the metrics and circuit-breaker state.
    """

    __slots__ = ("_cooldown", "_threshold", "providers")

    def __init__(
        self,
        providers: list[str],
        *,
        cooldown_seconds: float = 300.0,
        failure_threshold: int = 3,
    ) -> None:
        self.providers: dict[str, ProviderMetrics] = {
            p: ProviderMetrics(name=p) for p in providers
        }
        self._cooldown = float(cooldown_seconds)
        self._threshold = int(failure_threshold)

    def record_outcome(
        self, provider_name: str, latency: float, success: bool
    ) -> None:
        """Record a single call result; trips the circuit breaker on N consecutive failures."""
        p = self.providers.get(provider_name)
        if p is None:
            return
        if success:
            p.successes += 1
            p.consecutive_failures = 0
            p.latencies.append(latency)
        else:
            p.failures += 1
            p.consecutive_failures += 1
            if p.consecutive_failures >= self._threshold:
                p.cooldown_until = time.time() + self._cooldown

    def get_ranked_providers(self) -> list[str]:
        """Return available providers sorted best → worst."""
        valid = [p for p in self.providers.values() if p.is_available]
        valid.sort(key=lambda x: x.score, reverse=True)
        return [p.name for p in valid]
