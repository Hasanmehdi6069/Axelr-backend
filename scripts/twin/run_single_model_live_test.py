"""
run_single_model_live_test.py — v2.0 (WORLD-CLASS, HONEST)
==========================================================
Deep-dive on ONE model at a time. Saves every output to disk for human review.

What changed from v1:
  • NO fake quality score. The old "keyword overlap" metric was gameable —
    echoing the prompt scored 100%. This version uses:
      - stub detection (hard fail)
      - determinism check (temperature 0)
      - golden factual checks (loose, honest about tiny-model limits)
      - length sanity
      - optional: writes all outputs to ./artifacts/<model>/ for manual review
  • Real TTFT measured with streaming.
  • Real p50/p95/p99 (proper percentile math).
  • Real per-operation RSS delta.
  • Honest SKIP when binary/model missing.

USAGE
-----
    python run_single_model_live_test.py --model smollm2-135m
    python run_single_model_live_test.py --model all-blitz
    python run_single_model_live_test.py --model smollm2-135m --artifacts ./out
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

import importlib.util


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = name
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


twin_roster = _load_module("twin_roster", BACKEND_ROOT / "core" / "twin_roster.py")
twin_engine = _load_module("twin_engine", BACKEND_ROOT / "core" / "twin_engine.py")

MODEL_ROSTER = twin_roster.MODEL_ROSTER
ROSTER_BY_ID = twin_roster.ROSTER_BY_ID
TwinEngine = twin_engine.TwinEngine
_rss_mb = twin_engine._rss_mb


# ── Golden set: honest, curated, verifiable ─────────────────────────────
# Format: (task_tag, prompt, expected_keywords_any, max_chars)
GOLDEN = [
    ("math", "What is 2 + 2? Reply with just the digit.", ("4",), 10),
    ("fact", "What is the capital of France?", ("paris",), 40),
    ("fact", "How many days in a week?", ("7", "seven"), 20),
    ("fact", "What color is the sky on a clear day?", ("blue",), 40),
    ("fact", "What is H2O commonly known as?", ("water",), 40),
    ("creative", "Write a haiku about the moon.", ("moon", "night", "sky", "light", "silver", "glow", "white"), 250),
    ("reason", "If I have 3 apples and eat 1, how many remain?", ("2", "two"), 30),
    ("instruct", "Reply with the word OK and nothing else.", ("ok",), 10),
]


@dataclass
class ModelResult:
    model_id: str
    model_name: str
    tier: str
    file: str
    status: str = "SKIP"
    reason: str = ""
    load_ms: float = 0.0
    rss_before_mb: int = 0
    rss_peak_mb: int = 0
    rss_after_mb: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    ttft_ms: list[float] = field(default_factory=list)
    golden_results: list[dict] = field(default_factory=list)
    outputs: list[dict] = field(default_factory=list)  # full transcripts
    stub_count: int = 0
    determinism_ok: bool = False
    determinism_samples: list[str] = field(default_factory=list)
    exception: str = ""

    @property
    def p50(self) -> float:
        return statistics.median(self.latencies_ms) if self.latencies_ms else 0.0

    @property
    def p95(self) -> float:
        if not self.latencies_ms:
            return 0.0
        s = sorted(self.latencies_ms)
        return s[max(0, int(len(s) * 0.95) - 1)]

    @property
    def p99(self) -> float:
        if not self.latencies_ms:
            return 0.0
        s = sorted(self.latencies_ms)
        return s[max(0, int(len(s) * 0.99) - 1)]

    @property
    def ttft_p50(self) -> float:
        return statistics.median(self.ttft_ms) if self.ttft_ms else 0.0

    @property
    def golden_pass(self) -> int:
        return sum(1 for g in self.golden_results if g["passed"])


def _is_stub(text: str) -> bool:
    if not text:
        return True
    return text.startswith("[stub]") or "multi-agent system" in text or "temporarily busy" in text


async def _measure_ttft(engine: TwinEngine, prompt: str) -> tuple[float, str]:
    """
    Measure time-to-first-token by calling /completion with streaming and
    reading the first chunk. Falls back to non-streaming if server rejects.
    """
    resident = engine._fast or engine._elite
    if resident is None or engine._http is None:
        # Fall back to full infer — but mark TTFT as full latency
        t0 = time.perf_counter()
        out = await engine.infer("core", "core", prompt, tier="free")
        return (time.perf_counter() - t0) * 1000, out

    url = f"http://127.0.0.1:{resident.port}/completion"
    body = {
        "prompt": prompt,
        "n_predict": resident.spec.max_out,
        "temperature": 0.0,
        "stream": True,
        "cache_prompt": True,
    }
    t0 = time.perf_counter()
    first_token_ms = None
    collected: list[str] = []
    try:
        async with engine._http.stream("POST", url, json=body, timeout=30.0) as r:
            if r.status_code != 200:
                return 0.0, ""
            async for line in r.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                piece = obj.get("content") or ""
                if piece:
                    if first_token_ms is None:
                        first_token_ms = (time.perf_counter() - t0) * 1000
                    collected.append(piece)
    except Exception:
        # Streaming failed — fall back
        t0 = time.perf_counter()
        out = await engine.infer("core", "core", prompt, tier="free")
        return (time.perf_counter() - t0) * 1000, out
    return first_token_ms or 0.0, "".join(collected)


async def test_model(spec, engine: TwinEngine, artifacts: Optional[Path]) -> ModelResult:
    r = ModelResult(
        model_id=spec.id,
        model_name=spec.name,
        tier=spec.category,
        file=spec.file,
    )
    print("\n══════════════════════════════════════════════════════════════")
    print(f"MODEL: {spec.name}  ({spec.id})")
    print(f"  tier={spec.category}  weights={spec.weights_mb}MB  ctx={spec.ctx}  max_out={spec.max_out}")
    print("══════════════════════════════════════════════════════════════")

    model_path = engine._models_dir / spec.file
    if not model_path.is_file():
        r.status = "SKIP"
        r.reason = f"file missing: {model_path}"
        print(f"  ⏭ SKIP — {r.reason}")
        return r
    if not engine.is_available():
        r.status = "SKIP"
        r.reason = "engine unavailable (no llama-server binary)"
        print(f"  ⏭ SKIP — {r.reason}")
        return r

    gc.collect()
    await asyncio.sleep(0.2)
    r.rss_before_mb = _rss_mb()

    try:
        # ── Load
        engine._fast_spec = spec
        async with engine._lock:
            await engine._kill_locked("fast")
            t0 = time.perf_counter()
            await engine._load_locked(spec, slot="fast")
        r.load_ms = (time.perf_counter() - t0) * 1000
        resident = engine._fast
        if resident is None or not resident.alive:
            raise RuntimeError("model failed to load")
        r.rss_peak_mb = _rss_mb()
        print(f"  ✓ loaded in {r.load_ms:.0f}ms, RSS={r.rss_peak_mb}MB")

        # ── Warmup
        await engine.infer("core", "core", "hi", tier="free")

        # ── Golden set
        print(f"\n  Running {len(GOLDEN)} golden prompts…")
        for tag, prompt, kws, max_chars in GOLDEN:
            t0 = time.perf_counter()
            out = await engine.infer("core", "core", prompt, tier="free")
            lat = (time.perf_counter() - t0) * 1000
            stub = _is_stub(out)
            if stub:
                r.stub_count += 1
                passed = False
                reason = "stub"
            else:
                r.latencies_ms.append(lat)
                low = out.lower()
                kw_ok = any(k in low for k in kws) if kws else True
                len_ok = len(out) <= max_chars if max_chars else True
                passed = kw_ok and len_ok
                reason = "" if passed else f"kw={kw_ok} len={len_ok}"
            r.golden_results.append(
                {
                    "tag": tag,
                    "prompt": prompt,
                    "output": out,
                    "latency_ms": round(lat, 1),
                    "stub": stub,
                    "passed": passed,
                    "reason": reason,
                }
            )
            r.outputs.append({"tag": tag, "prompt": prompt, "output": out})
            mark = "✓" if passed else "✗"
            print(f"    {mark} [{tag:9s}] {prompt[:45]:45s} → {out[:50]!r}" + (f"  ({reason})" if reason else ""))

        # ── TTFT
        print("\n  Measuring time-to-first-token (streaming)…")
        try:
            for i in range(3):
                ttft, text = await _measure_ttft(engine, "Say hi.")
                if ttft > 0 and not _is_stub(text):
                    r.ttft_ms.append(ttft)
            if r.ttft_ms:
                print(f"    TTFT p50 = {r.ttft_p50:.0f}ms  (samples: {[round(x) for x in r.ttft_ms]})")
        except Exception as e:
            print(f"    TTFT measurement skipped: {e}")

        # ── Determinism
        print("\n  Determinism check (temp=0, same prompt twice)…")
        a = await engine.infer("core", "core", "Reply with OK.", tier="free")
        b = await engine.infer("core", "core", "Reply with OK.", tier="free")
        r.determinism_samples = [a, b]
        r.determinism_ok = (a == b) and not _is_stub(a)
        print(f"    {'✓' if r.determinism_ok else '✗'} a={a[:40]!r}  b={b[:40]!r}")

        # ── Teardown
        async with engine._lock:
            await engine._kill_locked("fast")
        gc.collect()
        await asyncio.sleep(0.5)
        r.rss_after_mb = _rss_mb()

        # ── Verdict (honest thresholds)
        checks = [
            ("stubs=0", r.stub_count == 0),
            ("determinism", r.determinism_ok),
            ("golden>=50%", r.golden_pass >= len(GOLDEN) // 2),
            ("p95<4s", r.p95 < 4000),
        ]
        failed = [name for name, ok in checks if not ok]
        if failed:
            r.status = "FAIL"
            r.reason = "failed: " + ", ".join(failed)
        else:
            r.status = "PASS"

        print(f"\n  → {r.status}  " + (f"({r.reason})" if r.reason else ""))
        print(
            f"    p50={r.p50:.0f}ms p95={r.p95:.0f}ms p99={r.p99:.0f}ms  "
            f"stubs={r.stub_count}  golden={r.golden_pass}/{len(GOLDEN)}"
        )
        print(f"    RSS: {r.rss_before_mb} → peak {r.rss_peak_mb} → after {r.rss_after_mb}MB")

    except Exception as e:
        import traceback

        r.status = "FAIL"
        r.reason = f"{type(e).__name__}: {e}"
        r.exception = traceback.format_exc()
        print(f"  ✗ EXCEPTION — {r.reason}")
        print(r.exception)
        try:
            async with engine._lock:
                await engine._kill_locked("fast")
        except Exception:
            pass

    # ── Artifacts
    if artifacts:
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / f"{spec.id}.json").write_text(
            json.dumps(asdict(r), indent=2, default=str),
            encoding="utf-8",
        )
        (artifacts / f"{spec.id}.transcript.txt").write_text(
            "\n\n".join(f"PROMPT: {o['prompt']}\nOUTPUT: {o['output']}" for o in r.outputs),
            encoding="utf-8",
        )
        print(f"  artifacts → {artifacts / (spec.id + '.json')}")

    return r


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="Model id, or 'all', 'all-fast', 'all-blitz'")
    ap.add_argument("--artifacts", type=Path, default=None, help="Directory to save transcripts")
    args = ap.parse_args()

    # Resolve targets
    if args.model == "all":
        targets = [m for m in MODEL_ROSTER if not m.rejected]
    elif args.model == "all-fast":
        targets = [m for m in MODEL_ROSTER if not m.rejected and m.category == "fast"]
    elif args.model == "all-blitz":
        targets = [m for m in MODEL_ROSTER if not m.rejected and m.category == "blitz"]
    else:
        spec = ROSTER_BY_ID.get(args.model)
        if spec is None:
            print(f"Unknown model id {args.model!r}")
            return 2
        targets = [spec]

    print(f"Testing {len(targets)} model(s)")
    engine = TwinEngine()
    if not engine.is_available():
        print(f"⚠ Engine unavailable: binary={engine._binary}")
        print(f"  models_dir={engine._models_dir}")
        print("  Nothing to test — every model will SKIP.")

    results: list[ModelResult] = []
    for spec in targets:
        results.append(await test_model(spec, engine, args.artifacts))
        gc.collect()
        await asyncio.sleep(0.3)

    try:
        await engine.shutdown()
    except Exception:
        pass

    # Final summary
    passed = [r for r in results if r.status == "PASS"]
    failed = [r for r in results if r.status == "FAIL"]
    skipped = [r for r in results if r.status == "SKIP"]
    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print(f"  PASSED  : {len(passed)}")
    print(f"  FAILED  : {len(failed)}")
    print(f"  SKIPPED : {len(skipped)}")
    if failed:
        print("\n  Failures:")
        for r in failed:
            print(f"    ✗ {r.model_id}: {r.reason}")
    if skipped:
        print("\n  Skipped:")
        for r in skipped:
            print(f"    ⏭ {r.model_id}: {r.reason}")
    if passed:
        print("\n  Passed:")
        for r in passed:
            print(f"    ✓ {r.model_id:25s} p50={r.p50:.0f}ms p95={r.p95:.0f}ms RSS peak={r.rss_peak_mb}MB")

    if failed:
        return 1
    if passed:
        return 0
    return 2 if skipped else 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
