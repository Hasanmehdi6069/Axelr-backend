"""Apex Router — policy-driven hybrid inference router."""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from .telemetry import log_route
from .tiers import BaseTier, FirebaseTier, LocalTier, NebiusTier, TierResult

DEFAULT_CONFIG = {
    "policy": "latency-first",
    "policies": {
        "latency-first": {
            "order": ["firebase", "local", "nebius"],
            "confidence_thresholds": {
                "firebase": 0.85, "local": 0.75, "nebius": 0.0,
            },
        },
        "cost-first": {
            "order": ["firebase", "local", "nebius"],
            "confidence_thresholds": {
                "firebase": 0.80, "local": 0.70, "nebius": 0.0,
            },
        },
        "quality-first": {
            "order": ["nebius", "local", "firebase"],
            "confidence_thresholds": {
                "nebius": 0.90, "local": 0.75, "firebase": 0.0,
            },
        },
    },
    "costs": {"firebase": 0.0, "local": 0.00002, "nebius": 0.0007},
}


# ─── Heuristic pre-classifier (task arrives untyped) ──────────────────────
_TASK_PATTERNS = [
    ("receipt", re.compile(
        r"receipt|invoice|subtotal|total\b|\$\d", re.I)),
    ("clause", re.compile(
        r"clause|section\s+\d|lessee|lessor|indemnif|shall\b|herein", re.I)),
    ("extraction", re.compile(
        r"extract|pull out|list the|find all|return all", re.I)),
    ("summarization", re.compile(
        r"summari[sz]e|summary|tl;?dr|abstract", re.I)),
    ("classification", re.compile(
        r"classify|label|categori[sz]e|is this a", re.I)),
]


def classify_task(prompt: str) -> str:
    """Cheap regex pre-classifier. Real classification happens inside tiers."""
    for task, pattern in _TASK_PATTERNS:
        if pattern.search(prompt):
            return task
    return "general"


# ─── Route result contract ────────────────────────────────────────────────
@dataclass(slots=True)
class RouteResult:
    text: str
    taskType: str
    tier: str
    confidence: float
    latencyMs: int
    costUsd: float
    policy: str
    escalated: bool
    cascade: list[dict]
    simulated: bool = False
    exhausted: bool = False

    def to_dict(self) -> dict:
        out = {
            "text": self.text,
            "taskType": self.taskType,
            "tier": self.tier,
            "confidence": self.confidence,
            "latencyMs": self.latencyMs,
            "costUsd": self.costUsd,
            "policy": self.policy,
            "escalated": self.escalated,
            "cascade": self.cascade,
            "simulated": self.simulated,
        }
        if self.exhausted:
            out["exhausted"] = True
        return out


# ─── Router ───────────────────────────────────────────────────────────────
class ApexRouter:
    """
    Policy-driven hybrid inference router.

        router = await ApexRouter.create(config_path=..., policy=...)
        result = await router.route(prompt, taskType=None)
        await router.close()
    """

    def __init__(self, config: dict, tiers: list[BaseTier],
                 policy: Optional[str] = None):
        self.config = config
        self.policy = policy or config.get("policy", "latency-first")
        self._tiers: dict[str, BaseTier] = {t.name: t for t in tiers}
        self._lock = asyncio.Lock()

    @classmethod
    async def create(cls, config_path: Optional[str | Path] = None,
                     policy: Optional[str] = None,
                     tiers_override: Optional[dict[str, BaseTier]] = None,
                     ) -> "ApexRouter":
        config = _load_config(config_path)
        if policy:
            config["policy"] = policy
        if config["policy"] not in config["policies"]:
            raise ValueError(f"unknown policy: {config['policy']}")

        costs = config.get("costs", {})
        tiers = tiers_override or {
            "firebase": FirebaseTier(
                cost_usd=float(costs.get("firebase", 0.0))),
            "local": LocalTier(
                cost_usd=float(costs.get("local", 0.00002))),
            "nebius": NebiusTier(
                cost_usd=float(costs.get("nebius", 0.0007))),
        }
        return cls(config, list(tiers.values()), policy=config["policy"])

    async def route(self, prompt: str, taskType: Optional[str] = None,
                    maxTokens: int = 512) -> RouteResult:
        if not prompt or not prompt.strip():
            raise ValueError("prompt is required")

        task = taskType or classify_task(prompt)
        policy_def = self.config["policies"][self.policy]
        order: list[str] = policy_def["order"]
        thresholds: dict[str, float] = policy_def.get(
            "confidence_thresholds", {})

        cascade: list[dict] = []
        last_result: Optional[TierResult] = None

        for tier_name in order:
            tier = self._tiers.get(tier_name)
            if tier is None:
                continue
            result = await tier.infer(prompt, taskType=task,
                                      maxTokens=maxTokens)
            cascade.append({
                "tier": tier_name,
                "confidence": round(result.confidence, 2),
                "latencyMs": result.latencyMs,
                "costUsd": result.costUsd,
                "simulated": result.simulated,
                **({"error": result.error} if result.error else {}),
            })

            if result.error:
                last_result = result
                continue

            last_result = result
            threshold = thresholds.get(tier_name, 0.0)
            if result.confidence >= threshold:
                return await self._finalize(
                    result, task, cascade, exhausted=False)

        if last_result is None:
            raise RuntimeError("no tiers available in policy order")
        return await self._finalize(
            last_result, task, cascade, exhausted=True)

    async def _finalize(self, result: TierResult, task: str,
                        cascade: list[dict], exhausted: bool) -> RouteResult:
        route = RouteResult(
            text=result.text,
            taskType=result.taskType or task,
            tier=result.tier,
            confidence=round(result.confidence, 3),
            latencyMs=result.latencyMs,
            costUsd=result.costUsd,
            policy=self.policy,
            escalated=len(cascade) > 1,
            cascade=cascade,
            simulated=result.simulated,
            exhausted=exhausted,
        )
        await log_route({
            "taskType": route.taskType,
            "tier": route.tier,
            "latencyMs": route.latencyMs,
            "costUsd": route.costUsd,
            "confidence": route.confidence,
            "policy": route.policy,
            "escalated": route.escalated,
            "cascade": [
                {"tier": c["tier"], "confidence": c["confidence"]}
                for c in cascade
            ],
            "simulated": route.simulated,
        })
        return route

    async def close(self) -> None:
        async with self._lock:
            await asyncio.gather(
                *(t.close() for t in self._tiers.values()),
                return_exceptions=True,
            )

    def tier_names(self) -> list[str]:
        return list(self._tiers.keys())

    def policies(self) -> list[str]:
        return list(self.config["policies"].keys())


# ─── Config loader ────────────────────────────────────────────────────────
def _load_config(path: Optional[str | Path]) -> dict:
    if path is None:
        path = Path.cwd() / "config" / "routing.yaml"
    path = Path(path)
    if not path.exists():
        return _deep_merge({}, DEFAULT_CONFIG)
    with open(path, "r", encoding="utf-8") as fh:
        user = yaml.safe_load(fh) or {}
    return _deep_merge(user, DEFAULT_CONFIG)


def _deep_merge(base: dict, defaults: dict) -> dict:
    out = dict(base)
    for k, v in defaults.items():
        if k not in out:
            out[k] = v
        elif isinstance(v, dict) and isinstance(out[k], dict):
            out[k] = _deep_merge(out[k], v)
    return out
