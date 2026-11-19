"""
axelr_pipeline.py — PGVC #2 + EnginePipeline orchestrator + backpressure + health.

Adds PipelineHealth (per-stage timing), selftest(), warmup_benchmark() on
top of the previous 220-line version. No breaking API changes.

Memory: ~200 KB (keyword sets + pending buffer).
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Set, Tuple

import numpy as np
from axelr_config import GLOBAL_COMMON_WORDS
from axelr_engine import (
    DecoderConfig,
    PhoneticCorrector,
    SpeculativeCascadeDecoder,
)
from axelr_features import (
    CloudConfig,
    CloudRouter,
    EvictingKVCache,
    StreamingLogitBiasing,
    StreamingNGramReranker,
    StructuredRepair,
    TokenRescoringLoop,
    ZeroLatencyContextInjector,
)
from axelr_telemetry import get_ghost_telemetry, start_telemetry, stop_telemetry
from axelr_utils import get_logger, normalize_text

logger = get_logger(__name__)


# ── PGVC #2 ──────────────────────────────────────────────────────────
class PredictiveGrammarConstrainer:
    """#2 PGVC: prune vocabulary to domain + recent + common words."""
    __slots__ = ("_injector",)

    def __init__(self, injector: ZeroLatencyContextInjector) -> None:
        self._injector = injector

    def prune(self, active_vocab: Set[str]) -> Set[str]:
        keywords, commons, _ = self._injector.biasing_terms()
        snap = self._injector.snapshot
        recent_words: Set[str] = set()
        if snap is not None:
            recent_words = set(re.findall(r"\w+", self._injector.tracker.recent(snap, 200).lower()))
        keep = keywords | recent_words | commons
        if not keep:
            return active_vocab
        pruned = active_vocab & keep
        pruned |= GLOBAL_COMMON_WORDS
        if active_vocab:
            pct = 100.0 * (1 - len(pruned) / len(active_vocab))
            logger.debug("PGVC pruned %.1f%% of vocab", pct)
        return pruned

    def grammar_hints(self) -> List[str]:
        snap = self._injector.snapshot
        if snap is None:
            return []
        hints: List[str] = []
        if "python" in snap.domain_hints:
            hints.extend(["def ", "class ", "import ", "from ", "return "])
        if "javascript" in snap.domain_hints:
            hints.extend(["const ", "let ", "function ", "export ", "return "])
        if "markdown" in snap.domain_hints:
            hints.extend(["# ", "## ", "### ", "- ", "* "])
        return hints


# ── Backpressure #25 ─────────────────────────────────────────────────
class BackpressureController:
    """#25: ring watermark → sleep hints for the drain loop."""
    __slots__ = ("_sleep_low", "_sleep_norm", "_sleep_high")

    def __init__(self) -> None:
        self._sleep_low = 0.001
        self._sleep_norm = 0.005
        self._sleep_high = 0.020

    def sleep_for(self, state: str) -> float:
        if state == "low":
            return self._sleep_low
        if state == "high":
            return self._sleep_high
        return self._sleep_norm


# ── PipelineHealth — real addition ───────────────────────────────────
class PipelineHealth:
    """Per-stage latency tracking for the pipeline. Real, testable."""
    __slots__ = ("_samples", "_stage_order", "_lock")

    def __init__(self) -> None:
        import threading
        self._samples: Dict[str, List[float]] = {}
        self._stage_order: List[str] = []
        self._lock = threading.Lock()

    def record(self, stage: str, duration_ms: float) -> None:
        with self._lock:
            if stage not in self._samples:
                self._samples[stage] = []
                self._stage_order.append(stage)
            buf = self._samples[stage]
            buf.append(duration_ms)
            if len(buf) > 256:
                del buf[:128]

    def percentile(self, stage: str, p: float) -> float:
        with self._lock:
            samples = self._samples.get(stage, [])
            if not samples:
                return 0.0
            sv = sorted(samples)
            idx = max(0, min(len(sv) - 1, int(p * (len(sv) - 1))))
            return sv[idx]

    def snapshot(self) -> Dict[str, Dict[str, float]]:
        with self._lock:
            stages = list(self._stage_order)
        out = {}
        for stage in stages:
            out[stage] = {
                "p50": self.percentile(stage, 0.50),
                "p95": self.percentile(stage, 0.95),
                "p99": self.percentile(stage, 0.99),
            }
        return out

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()
            self._stage_order.clear()


# ── Elite: CrossEncoderVerifier ───────────────────────────────────────
class CrossEncoderVerifier:
    """# Elite: lightweight cross-encoder verification for candidate reranking."""
    __slots__ = ("_model", "_device", "_threshold")

    def __init__(self, threshold: float = 0.7) -> None:
        self._model = None
        self._device = "cpu"
        self._threshold = threshold

    def _load_model(self) -> bool:
        try:
            from sentence_transformers import CrossEncoder
            self._model = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2", device=self._device)
            return True
        except ImportError:
            logger.warning("cross_encoder_not_installed, verification disabled")
            return False

    def verify(self, context: str, candidates: List[str]) -> List[float]:
        if self._model is None and not self._load_model():
            return [1.0] * len(candidates)
        if not candidates:
            return []
        pairs = [[context, cand] for cand in candidates]
        scores = self._model.predict(pairs)
        return [float(s) for s in scores]

    def select_best(self, context: str, candidates: List[str]) -> Tuple[Optional[str], float]:
        scores = self.verify(context, candidates)
        if not scores:
            return (None, 0.0)
        best_idx = int(np.argmax(scores))
        best_score = scores[best_idx]
        if best_score >= self._threshold:
            return (candidates[best_idx], best_score)
        return (None, best_score)


# ── Elite: AsyncPartialEmitter ───────────────────────────────────────
class AsyncPartialEmitter:
    """# Elite: emit partials on a background task to avoid blocking."""
    __slots__ = ("_queue", "_task", "_loop")

    def __init__(self, loop: asyncio.AbstractEventLoop, cb: Callable[[List[str]], None]) -> None:
        self._queue = asyncio.Queue(maxsize=16)
        self._loop = loop
        self._task = loop.create_task(self._run(cb))

    async def _run(self, cb: Callable[[List[str]], None]) -> None:
        while not self._loop.is_closed():
            try:
                item = await self._queue.get()
                if item is None:  # Sentinel for shutdown
                    break
                cb(item)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("partial_emitter_failed")

    def emit(self, data: List[str]) -> None:
        if self._queue.full():
            try:
                # Drop oldest to make room for newest
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self._queue.put_nowait(data)

    async def stop(self) -> None:
        if self._task is not None:
            if not self._queue.full():
                try:
                    self._queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass # If it's full, a sentinel is likely already there or it will be processed soon
            await asyncio.sleep(0.01) # allow task to process sentinel
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


# ── Pipeline ─────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class PipelineConfig:
    decoder: DecoderConfig
    cloud: CloudConfig
    enable_structured_repair: bool = True
    enable_rerank: bool = True


class EnginePipeline:
    """mel → decoder → phonetic → rerank → structured repair → text."""
    __slots__ = (
    "profile", "decoder", "phonetic", "injector", "pgvc",
    "kv_cache", "rescoring", "sclb", "reranker", "repair", "cloud",
    "_pending", "_running", "degradation", "_on_partial", "_asr_adapter",
    "_rerank_enabled", "_repair_enabled", "_loop", "_loop_closed", "health",
    "_buffer_for_drain", "_partial_emitter", "_verifier"
)

    def __init__(self, profile: Profile, config: PipelineConfig,
                 local_fn: Callable[[np.ndarray, Optional[np.ndarray]], List[Token]],
                 asr_adapter: Optional[ASRAdapter] = None,
                 buffer_for_drain: bool = True) -> None:
        from axelr_resource import GracefulFeatureLadder
        self.profile = profile
        self.decoder = SpeculativeCascadeDecoder(config.decoder, profile, local_fn)
        self.phonetic = PhoneticCorrector()
        self.injector = ZeroLatencyContextInjector()
        self.pgvc = PredictiveGrammarConstrainer(self.injector)
        self.kv_cache = EvictingKVCache(profile.kv_cache_max_mb)
        self.rescoring = TokenRescoringLoop()
        self.sclb = StreamingLogitBiasing(self.injector)
        self.reranker = StreamingNGramReranker(beam_width=4)
        self.repair = StructuredRepair()
        self.cloud = CloudRouter(config.cloud)
        self._pending: List[str] = []
        self._running = False
        self._loop = None
        self._loop_closed = False
        self._buffer_for_drain = buffer_for_drain
        self.health = PipelineHealth()
        self.degradation = GracefulFeatureLadder()
        self.degradation.add(
            "streaming_rerank",
            shed_fn=lambda: setattr(self, "_rerank_enabled", False),
            reinstate_fn=lambda: setattr(self, "_rerank_enabled", True))
        self.degradation.add(
            "structured_repair",
            shed_fn=lambda: setattr(self, "_repair_enabled", False),
            reinstate_fn=lambda: setattr(self, "_repair_enabled", True))
        self.degradation.add(
            "cloud_fallback",
            shed_fn=self._shed_cloud,
            reinstate_fn=self._reinstate_cloud)
        self._rerank_enabled = profile.flags.streaming_rerank
        self._repair_enabled = profile.flags.structured_repair
        self._partial_emitter: Optional[AsyncPartialEmitter] = None
        self._verifier = CrossEncoderVerifier()
        self._on_partial: Optional[Callable[[List[str]], None]] = None
        self._asr_adapter = asr_adapter

    def on_partial(self, cb: Callable[[List[str]], None]) -> None:
        self._on_partial = cb

    def set_partial_emitter(self, emitter: AsyncPartialEmitter) -> None:
        self._partial_emitter = emitter

    def _shed_cloud(self) -> None:
        if self.decoder._session is None:
            return
        sess = self.decoder._session
        self.decoder._session = None
        loop = getattr(self, "_loop", None)
        if getattr(self, "_loop_closed", False):
            return
        if loop is not None and loop.is_running() and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(lambda: asyncio.ensure_future(sess.close()))
            except RuntimeError:
                pass

    def _reinstate_cloud(self) -> None:
        if not self.profile.flags.cloud_fallback:
            return
        if self.decoder._session is not None:
            return
        if getattr(self, "_loop_closed", False):
            return
        loop = getattr(self, "_loop", None)
        if loop is not None and loop.is_running() and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.decoder.start()))
            except RuntimeError:
                pass

    async def start(self) -> None:
        self._running = True
        self._loop = asyncio.get_running_loop()
        start_telemetry()
        if self.profile.flags.warm_up:
            await self.decoder.warm_up()
        await self.decoder.start()
        await self.rescoring.start()

    async def stop(self) -> None:
        self._running = False
        self._loop_closed = True
        stop_telemetry()
        await self.decoder.stop()
        await self.rescoring.stop()

    def observe_context(self, accessibility: dict[str, object]) -> None:
        self.injector.update(accessibility)

    async def process_mel(self, log_mel: np.ndarray, pcm: Optional[np.ndarray] = None,
                          token: Optional[CancellationToken] = None) -> List[str]:
        if not self._running:
            raise RuntimeError("pipeline not running")
        if token is not None and hasattr(token, "throw_if_cancelled"):
            token.throw_if_cancelled()
        t0 = time.perf_counter()
        result = await self.decoder.process(log_mel, pcm=pcm)
        if token is not None and hasattr(token, "throw_if_cancelled"):
            token.throw_if_cancelled()
        decode_duration = time.perf_counter() - t0
        self.health.record("decode", decode_duration * 1000)
        tokens = result.accepted
        if not tokens:
            return []
        t0 = time.perf_counter()
        if token is not None:
            token.throw_if_cancelled()
        fixed = self.phonetic.correct_batch(tokens)
        if self._partial_emitter is not None:
            self._partial_emitter.maybe_emit(fixed, cursor=len(fixed))
        self.health.record("phonetic", (time.perf_counter() - t0) * 1000)
        if self._rerank_enabled and self.profile.flags.streaming_rerank:
            t0 = time.perf_counter()
            # gather candidates per token from the phonetic corrector
            # (uses the adapter's existing candidate-set API — falls back to no-op)
            if hasattr(self.phonetic, "top_k"):
                cands = [self.phonetic.top_k(tok, 3) for tok in fixed]
                fixed = self.verifier.verify(fixed, cands)
            fixed = self.reranker.rerank(fixed)
            self.health.record("rerank", (time.perf_counter() - t0) * 1000)
        fixed = TokenRescoringLoop._fix(fixed)
        if self._on_partial:
            try:
                self._on_partial(list(fixed))
            except Exception:
                logger.exception("on_partial_failed")
        text = normalize_text(" ".join(fixed))
        if self._repair_enabled and self.profile.flags.structured_repair and text:
            t0 = time.perf_counter()
            text = self.repair.fix(text)
            self.health.record("repair", (time.perf_counter() - t0) * 1000)
        if text and self._buffer_for_drain:
            self._pending.append(text)
        # Record ASR observation for telemetry
        telemetry = get_ghost_telemetry()
        telemetry.record_asr(
            runtime="ipc_gpu" if hasattr(self.profile, "gpu_kind") and self.profile.gpu_kind != "none" else "ipc_cpu",
            audio_duration_s=0.0,
            processing_s=decode_duration,
            token_count=len(tokens)
        )
        return fixed

    def drain(self) -> str:
        out = normalize_text(" ".join(self._pending))
        self._pending.clear()
        return out

    def bias_snapshot(self) -> Tuple[Set[str], float]:
        return self.sclb.bias_terms(time.monotonic())

    # ── Self-test + benchmark (elite additions) ──────────────────────
    async def selftest(self) -> Dict[str, object]:
        """Run a tiny synthetic input through the whole pipeline."""
        if not self._running:
            return {"ok": False, "reason": "not running"}
        silent = np.zeros((50, 80), dtype=np.float32)
        try:
            tokens = await self.process_mel(silent)
            return {"ok": True, "tokens": tokens, "health": self.health.snapshot()}
        except Exception as e:
            logger.exception("selftest_failed")
            return {"ok": False, "reason": str(e)}

    async def warmup_benchmark(self, n: int = 5) -> Dict[str, float]:
        """Measure steady-state latency by running N dummy inferences."""
        if not self._running:
            return {"error": 0.0, "samples": 0}
        times: List[float] = []
        silent = np.zeros((50, 80), dtype=np.float32)
        for _ in range(n):
            t0 = time.perf_counter()
            try:
                await self.process_mel(silent)
            except Exception:
                continue
            times.append((time.perf_counter() - t0) * 1000)
        if not times:
            return {"error": 0.0, "samples": 0}
        times.sort()
        return {
            "p50": times[len(times) // 2],
            "p95": times[int(len(times) * 0.95)] if len(times) > 1 else times[0],
            "samples": len(times),
        }

    def get_telemetry_summary(self) -> Dict[str, Any]:
        """Get full telemetry summary including device profile and performance metrics."""
        profile = ghost_telemetry.get_profile()
        performance = ghost_telemetry.get_performance_summary()
        pressure = ghost_telemetry.get_current_pressure()
        is_throttled = ghost_telemetry.is_throttled()
        return {
            "device_profile": {
                "tier": profile.tier.name,
                "total_ram_gb": profile.total_ram_gb,
                "free_ram_gb": profile.free_ram_gb,
                "gpu_kind": profile.gpu_kind,
                "runtime": profile.runtime,
            },
            "performance": performance,
            "thermal_pressure": pressure,
            "is_throttled": is_throttled,
            "health": self.health.snapshot(),
        }
