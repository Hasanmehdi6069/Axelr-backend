"""Apex Router — tier abstractions.

Four tier implementations behind a uniform TierResult contract:

  FirebaseTier — on-device Gemini Nano via Firebase AI Logic (Node sidecar)
  LocalTier    — Gemma via Ollama  (Gemma 4 hackathon tier)
  NebiusTier   — cloud burst consuming axelr_nebius_nvidia.stream_inference

Every tier returns the same dataclass so the router never branches on name.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

import aiohttp


# ─── Import axelr_nebius_nvidia.core, trying common layouts ───────────────
def _try_import_nebius():
    """
    The Nebius module uses a relative import (`...core.elite_memory_optimizer`)
    that requires a specific package layout. We try three import strategies,
    degrading gracefully to the simulated fallback rather than failing hard.
    """
    # Strategy 1: module is already importable
    try:
        from axelr_nebius_nvidia.core import (  # type: ignore
            stream_inference, is_configured,
        )
        return stream_inference, is_configured, True
    except ImportError:
        pass

    # Strategy 2: sibling at <ancestor>/axelr_nebius_nvidia/ with <ancestor>/core/
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        candidate = ancestor / "axelr_nebius_nvidia"
        if (candidate / "core.py").exists():
            added = str(ancestor)
            sys.path.insert(0, added)
            try:
                from axelr_nebius_nvidia.core import (  # type: ignore
                    stream_inference, is_configured,
                )
                return stream_inference, is_configured, True
            except ImportError:
                sys.path.remove(added)
                continue

    # Strategy 3: not available — fallback path
    return None, (lambda: False), False


nebius_stream_inference, nebius_is_configured, _NEBIUS_AVAILABLE = _try_import_nebius()


# ─── Result contract ──────────────────────────────────────────────────────
@dataclass(slots=True)
class TierResult:
    text: str
    taskType: str
    confidence: float
    tier: str
    latencyMs: int
    costUsd: float
    simulated: bool = False
    error: Optional[str] = None

    def to_dict(self) -> dict:
        out = {
            "text": self.text, "taskType": self.taskType,
            "confidence": self.confidence, "tier": self.tier,
            "latencyMs": self.latencyMs, "costUsd": self.costUsd,
            "simulated": self.simulated,
        }
        if self.error:
            out["error"] = self.error
        return out


# ─── Prompt + parse helpers ───────────────────────────────────────────────
PROMPT_TEMPLATE = (
    "Return STRICT JSON only, no markdown, no preamble:\n"
    '{"taskType": "receipt|clause|classification|summarization|extraction|general", '
    '"confidence": <0..1>, "text": "<answer to the user>"}\n\n'
    "Input:\n{prompt}"
)

_JSON_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_JSON_OBJECT = re.compile(r"\{[\s\S]*\}")


def parse_strict_json(raw: str) -> dict:
    """Best-effort JSON extraction that tolerates fences and prose prefixes."""
    if not isinstance(raw, str) or not raw.strip():
        return {}
    cleaned = _JSON_FENCE.sub("", raw).strip()
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    match = _JSON_OBJECT.search(raw)
    if match:
        try:
            obj = json.loads(match.group(0))
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    return {}


def _clamp(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, f))


def _now_ms() -> float:
    return time.perf_counter() * 1000.0


def _hash_jitter(prompt: str, span: int) -> int:
    """Deterministic per-prompt integer in [0, span) — used for simulated variance."""
    h = hashlib.md5(prompt.encode("utf-8")).hexdigest()[:8]
    return int(h, 16) % span


# ─── Base tier ────────────────────────────────────────────────────────────
class BaseTier:
    name: str = "base"
    costUsd: float = 0.0

    async def infer(self, prompt: str, taskType: Optional[str] = None,
                    maxTokens: int = 512) -> TierResult:
        raise NotImplementedError

    def is_available(self) -> bool:
        return True

    async def close(self) -> None:
        pass


# ─── Firebase tier (on-device via Node.js sidecar) ────────────────────────
class FirebaseTier(BaseTier):
    """
    Firebase AI Logic on-device tier (BuildHybridAI).

    The Firebase AI Logic SDK is JavaScript-only; it cannot run on-device
    inside a Python process. We delegate to a small Node.js HTTP sidecar
    (`apex/firebase_bridge.mjs`) that holds the real SDK.

    On a real hybrid deployment (Chrome/Android WebView), this same code
    path runs natively — the SDK is genuinely on-device. Here it's a
    localhost hop so the router can compose all three tiers in one process.

    Fallback: if the sidecar isn't reachable (Node-only CI without Firebase
    credentials), we emit a heuristic result tagged `simulated: true` so
    demos run identically. Set `APEX_FIREBASE_MODE=strict` to hard-fail.
    """
    name = "firebase"

    def __init__(self, sidecar_url: Optional[str] = None,
                 cost_usd: float = 0.0):
        self.sidecar_url = sidecar_url or os.environ.get(
            "FIREBASE_SIDECAR_URL", "http://127.0.0.1:9091")
        self.costUsd = cost_usd
        self.strict = os.environ.get("APEX_FIREBASE_MODE", "") == "strict"

    def is_available(self) -> bool:
        return True  # always available (simulated fallback)

    async def infer(self, prompt: str, taskType: Optional[str] = None,
                    maxTokens: int = 512) -> TierResult:
        start = _now_ms()
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=8.0)
            ) as session:
                async with session.post(
                    urljoin(self.sidecar_url, "/infer"),
                    json={"prompt": prompt, "maxTokens": maxTokens},
                ) as resp:
                    if not resp.ok:
                        body = await resp.text()
                        raise RuntimeError(
                            f"sidecar_http_{resp.status}: {body[:200]}")
                    data = await resp.json()
            parsed = data if "confidence" in data else parse_strict_json(
                data.get("raw", ""))
            return TierResult(
                text=parsed.get("text", ""),
                taskType=parsed.get("taskType") or taskType or "general",
                confidence=_clamp(parsed.get("confidence", 0.5)),
                tier=self.name,
                latencyMs=int(_now_ms() - start),
                costUsd=self.costUsd,
                simulated=bool(parsed.get("simulated", False)),
            )
        except Exception as exc:
            if self.strict:
                return TierResult(
                    text="", taskType=taskType or "general", confidence=0.0,
                    tier=self.name, latencyMs=int(_now_ms() - start),
                    costUsd=self.costUsd, error=str(exc),
                )
            return await self._simulated(prompt, taskType, start, exc)

    async def _simulated(self, prompt: str, taskType: Optional[str],
                         start: float, exc: Exception) -> TierResult:
        # Deterministic latency ~80–110ms, mimicking on-device NSFW-safe Nano.
        await asyncio.sleep(0.080 + _hash_jitter(prompt, 30) / 1000.0)
        p = prompt.lower()
        task, conf = "general", 0.5
        if re.search(r"receipt|invoice|total|\$", p):
            task, conf = "receipt", 0.92
        elif re.search(r"clause|lessee|lessor|agreement|shall|indemnif", p):
            task, conf = "clause", 0.61
        elif re.search(r"extract|date|party|ref", p):
            task, conf = "extraction", 0.88
        elif re.search(r"summari[sz]e|summary|tl;?dr", p):
            task, conf = "summarization", 0.78
        return TierResult(
            text=f"[simulated on-device] {task}",
            taskType=task, confidence=conf, tier=self.name,
            latencyMs=int(_now_ms() - start), costUsd=self.costUsd,
            simulated=True, error=f"sidecar_unavailable: {exc}",
        )


# ─── Local tier (Gemma via Ollama) ────────────────────────────────────────
class LocalTier(BaseTier):
    """
    Local Gemma tier via Ollama (Gemma 4 hackathon tier).

    Ollama must be running with a Gemma model pulled:
        ollama pull gemma2:2b
        ollama serve
    Set GEMMA_MODEL to override (default gemma2:2b).
    """
    name = "local"

    def __init__(self, base_url: Optional[str] = None,
                 model: Optional[str] = None, cost_usd: float = 0.00002):
        self.base_url = base_url or os.environ.get(
            "OLLAMA_BASE_URL", "http://localhost:11434")
        self.model = model or os.environ.get("GEMMA_MODEL", "gemma2:2b")
        self.costUsd = cost_usd
        self.strict = os.environ.get("APEX_LOCAL_MODE", "") == "strict"

    async def infer(self, prompt: str, taskType: Optional[str] = None,
                    maxTokens: int = 512) -> TierResult:
        start = _now_ms()
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30.0)
            ) as session:
                async with session.post(
                    urljoin(self.base_url, "/api/generate"),
                    json={
                        "model": self.model,
                        "prompt": PROMPT_TEMPLATE.format(prompt=prompt),
                        "stream": False,
                        "format": "json",
                        "options": {"temperature": 0.3,
                                    "num_predict": maxTokens},
                    },
                ) as resp:
                    if not resp.ok:
                        body = await resp.text()
                        raise RuntimeError(
                            f"ollama_http_{resp.status}: {body[:200]}")
                    data = await resp.json()
            parsed = parse_strict_json(data.get("response", ""))
            return TierResult(
                text=parsed.get("text", ""),
                taskType=parsed.get("taskType") or taskType or "general",
                confidence=_clamp(parsed.get("confidence", 0.5)),
                tier=self.name,
                latencyMs=int(_now_ms() - start),
                costUsd=self.costUsd,
            )
        except Exception as exc:
            if self.strict:
                return TierResult(
                    text="", taskType=taskType or "general", confidence=0.0,
                    tier=self.name, latencyMs=int(_now_ms() - start),
                    costUsd=self.costUsd, error=str(exc),
                )
            return await self._simulated(prompt, taskType, start, exc)

    async def _simulated(self, prompt: str, taskType: Optional[str],
                         start: float, exc: Exception) -> TierResult:
        await asyncio.sleep(0.220 + _hash_jitter(prompt, 60) / 1000.0)
        p = prompt.lower()
        # Clause confidence sits just above cost-first threshold (0.70) so
        # some simulated clauses accept at local (matching the demo split).
        task, conf = "general", 0.65
        if re.search(r"receipt|invoice|\$", p):
            task, conf = "receipt", 0.78
        elif re.search(r"clause|lessee|shall|agreement", p):
            task, conf = "clause", 0.72
        elif re.search(r"extract|date|party", p):
            task, conf = "extraction", 0.82
        elif re.search(r"summari[sz]e", p):
            task, conf = "summarization", 0.75
        return TierResult(
            text=f"[simulated gemma] {task}",
            taskType=task, confidence=conf, tier=self.name,
            latencyMs=int(_now_ms() - start), costUsd=self.costUsd,
            simulated=True, error=f"ollama_unavailable: {exc}",
        )


# ─── Nebius tier (cloud burst via axelr_nebius_nvidia) ────────────────────
class NebiusTier(BaseTier):
    """
    Cloud burst tier — Nebius A100.

    Directly consumes `axelr_nebius_nvidia.core.stream_inference`, inheriting
    its per-region circuit breakers, token bucket, and connection pool. No
    HTTP hop between router and bridge — they share a process.

    The bridge streams free-form text; we aggregate the stream and parse a
    JSON envelope. If the model returns prose instead of JSON, we still
    surface the text and tag confidence by length so the router's threshold
    logic still functions.
    """
    name = "nebius"

    def __init__(self, model: Optional[str] = None,
                 cost_usd: float = 0.0007):
        self.model = model or os.environ.get("NEBIUS_MODEL", "llama3:70b")
        self.costUsd = cost_usd
        self.strict = os.environ.get("APEX_NEBIUS_MODE", "") == "strict"

    def is_available(self) -> bool:
        return _NEBIUS_AVAILABLE and nebius_is_configured()

    async def infer(self, prompt: str, taskType: Optional[str] = None,
                    maxTokens: int = 1024) -> TierResult:
        start = _now_ms()

        if not _NEBIUS_AVAILABLE:
            if self.strict:
                return TierResult(
                    text="", taskType=taskType or "general", confidence=0.0,
                    tier=self.name, latencyMs=0, costUsd=self.costUsd,
                    error="axelr_nebius_nvidia module not importable",
                )
            return await self._simulated(prompt, taskType, start,
                                         RuntimeError("module_missing"))

        try:
            chunks: list[str] = []
            async for chunk in nebius_stream_inference(
                prompt=PROMPT_TEMPLATE.format(prompt=prompt),
                model=self.model,
                max_tokens=maxTokens,
                temperature=0.3,
            ):
                chunks.append(chunk)
            raw = "".join(chunks)
            parsed = parse_strict_json(raw)
            if not parsed:
                # Model returned prose; surface it, tag by length.
                conf = min(0.85, 0.5 + len(raw) / 4000.0)
                return TierResult(
                    text=raw.strip(), taskType=taskType or "general",
                    confidence=conf, tier=self.name,
                    latencyMs=int(_now_ms() - start), costUsd=self.costUsd,
                )
            return TierResult(
                text=parsed.get("text", raw.strip()),
                taskType=parsed.get("taskType") or taskType or "general",
                confidence=_clamp(parsed.get("confidence", 0.85)),
                tier=self.name,
                latencyMs=int(_now_ms() - start),
                costUsd=self.costUsd,
            )
        except Exception as exc:
            if self.strict:
                return TierResult(
                    text="", taskType=taskType or "general", confidence=0.0,
                    tier=self.name, latencyMs=int(_now_ms() - start),
                    costUsd=self.costUsd, error=str(exc),
                )
            return await self._simulated(prompt, taskType, start, exc)

    async def _simulated(self, prompt: str, taskType: Optional[str],
                         start: float, exc: Exception) -> TierResult:
        await asyncio.sleep(1.10 + _hash_jitter(prompt, 300) / 1000.0)
        p = prompt.lower()
        task, conf = "general", 0.85
        if re.search(r"receipt|\$", p):
            task, conf = "receipt", 0.94
        elif re.search(r"clause|lessee|shall", p):
            task, conf = "clause", 0.91
        elif re.search(r"extract|date|party", p):
            task, conf = "extraction", 0.96
        elif re.search(r"summari[sz]e", p):
            task, conf = "summarization", 0.89
        return TierResult(
            text=f"[simulated nebius] {task}",
            taskType=task, confidence=conf, tier=self.name,
            latencyMs=int(_now_ms() - start), costUsd=self.costUsd,
            simulated=True, error=f"nebius_unavailable: {exc}",
        )