"""axelr_main.py — Entry point with preflight · subcommands · health report.

Honest contract: refuses to run if no real ASR adapter is available
unless AXELR_ALLOW_MOCK=1 is explicitly set.

New in v4.2:
  * PreflightChecker — verifies python/deps/models/env before start
  * --preflight subcommand (dry run, exit 0/1)
  * --config file loading
  * --dry-run mode
  * print_health_report() for operational visibility
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional

from axelr_adapters import ASRConfig, make_local_fn, select_adapter
from axelr_audio import (
    MOD_CONTROL,
    MOD_SHIFT,
    AudioCapturePipeline,
    Hotkey,
    HotkeyInterceptor,
    PermissionType,
    ZeroTrustPermissionManager,
)
from axelr_config import (
    HARDWARE_PROFILES,
    Tier,
    apply_env_overrides,
    budget_for,
    profile_summary,
    select_profile,
    validate_all_profiles,
)
from axelr_devices import (
    DeviceTier,
    FeatureUnavailableError,
    classify,
    require_feature,
    user_facing_summary,
)
from axelr_engine import DecoderConfig
from axelr_features import CloudConfig
from axelr_pipeline import BackpressureController, EnginePipeline, PipelineConfig
from axelr_resource import (
    ColdStartProfiler,
    MemoryPressureMonitor,
    PressureConfig,
)
from axelr_ring import RingBufferConfig, ZeroCopyRingBuffer
from axelr_utils import MetricsCollector, get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# PreflightChecker — verify environment before start
# ═══════════════════════════════════════════════════════════════════════
@dataclass
class PreflightResult:
    ok: bool
    checks: List[Dict[str, Any]] = field(default_factory=list)

    def fail(self, name: str, detail: str) -> None:
        self.checks.append({"name": name, "ok": False, "detail": detail})
        self.ok = False

    def pass_(self, name: str, detail: str = "") -> None:
        self.checks.append({"name": name, "ok": True, "detail": detail})

    def summary(self) -> str:
        lines = ["Preflight checks:"]
        for c in self.checks:
            marker = "✓" if c["ok"] else "✗"
            lines.append(f"  {marker} {c['name']}  {c.get('detail', '')}")
        lines.append(f"Result: {'PASS' if self.ok else 'FAIL'}")
        return "\n".join(lines)


class PreflightChecker:
    """Verifies python version, deps, model availability, env before start."""

    REQUIRED_PY = (3, 9)
    REQUIRED_DEPS = ("numpy",)

    def run(self, tier: Optional[Tier] = None) -> PreflightResult:
        result = PreflightResult(ok=True)
        self._check_python(result)
        self._check_deps(result)
        self._check_psutil(result)
        self._check_device(result)
        self._check_profiles(result)
        self._check_adapter(result)
        self._check_env(result)
        return result

    def _check_device(self, r: PreflightResult) -> None:
        profile = classify()
        if profile.tier == DeviceTier.MOBILE:
            r.fail("device_tier",
                   "Mobile-tier device — standalone Python mode not supported. "
                   "Use --mode browser-only.")
            return
        if profile.tier < DeviceTier.STANDARD:
            r.pass_("device_tier",
                   f"{profile.tier.label} — will run whisper-tiny on CPU")
        else:
            r.pass_("device_tier",
                   f"{profile.tier.label} — full features available")

    def _check_python(self, r: PreflightResult) -> None:
        if sys.version_info >= self.REQUIRED_PY:
            r.pass_("python_version", f"{sys.version_info.major}.{sys.version_info.minor}")
        else:
            r.fail("python_version", f"needs {self.REQUIRED_PY}, got {sys.version_info[:2]}")

    def _check_deps(self, r: PreflightResult) -> None:
        for dep in self.REQUIRED_DEPS:
            try:
                __import__(dep)
                r.pass_(f"dep_{dep}", "available")
            except ImportError:
                r.fail(f"dep_{dep}", "missing")

    def _check_psutil(self, r: PreflightResult) -> None:
        try:
            import psutil  # noqa: F401
            r.pass_("psutil", "available")
        except ImportError:
            r.pass_("psutil", "missing (explicit tier required)")

    def _check_profiles(self, r: PreflightResult) -> None:
        ok, issues = validate_all_profiles()
        if ok:
            r.pass_("profiles", f"{len(HARDWARE_PROFILES)} valid")
        else:
            r.fail("profiles", f"{len(issues)} invalid")

    def _check_adapter(self, r: PreflightResult) -> None:
        try:
            adapter = select_adapter(ASRConfig())
            if adapter.name == "mock" and os.environ.get("AXELR_ALLOW_MOCK") != "1":
                r.fail("asr_adapter", "mock without AXELR_ALLOW_MOCK=1")
            else:
                r.pass_("asr_adapter", adapter.name)
        except RuntimeError as e:
            r.fail("asr_adapter", str(e)[:80])

    def _check_env(self, r: PreflightResult) -> None:
        home = os.environ.get("AXELR_HOME")
        if home:
            path = Path(home)
            try:
                path.mkdir(parents=True, exist_ok=True)
                r.pass_("axelr_home", str(path))
            except Exception as e:
                r.fail("axelr_home", str(e)[:80])
        else:
            r.pass_("axelr_home", "default")


def print_health_report(
    profile,
    budget,
    adapter_name: str,
    cold_start: Optional[ColdStartProfiler] = None,
) -> None:
    print("\n" + "=" * 60)
    print("AXELR HEALTH REPORT")
    print("=" * 60)
    print(f"  Profile:     {profile_summary(profile)}")
    print(f"  Budget:      {budget.budget_mb} MB (headroom {budget.headroom_mb} MB)")
    print(f"  ASR model:   {budget.asr_model_size}")
    print(f"  ASR adapter: {adapter_name}")
    print(f"  Mode:        {profile.tier.value}")
    if cold_start:
        rep = cold_start.report()
        print(f"  Cold start:  {'HEALTHY' if rep['healthy'] else 'VIOLATIONS'}")
        for name, ms in rep["measured"].items():
            print(f"    {name:20s} {ms:8.1f} ms")
    print("=" * 60 + "\n")


# ═══════════════════════════════════════════════════════════════════════
# Config loading
# ═══════════════════════════════════════════════════════════════════════
def load_config_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        logger.warning("config_file_missing path=%s", path)
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("config_file_invalid")
        return {}


# ═══════════════════════════════════════════════════════════════════════
# Drain loop
# ═══════════════════════════════════════════════════════════════════════
async def _drain_loop(
    capture: AudioCapturePipeline,
    pipeline: EnginePipeline,
    metrics: MetricsCollector,
    stop_event: asyncio.Event,
) -> None:
    bp = BackpressureController()
    while not stop_event.is_set():
        t0 = metrics.now()
        mel = await capture.read_batch()
        if mel is None:
            await asyncio.sleep(bp.sleep_for(capture.backpressure().value))
            continue
        try:
            await pipeline.process_mel(mel)
        except Exception:
            logger.exception("pipeline.process_mel failed")
        metrics.observe("drain_ms", metrics.elapsed_ms(t0))
        if capture.endpoint_fired():
            metrics.observe("endpoint", 1.0)
        await asyncio.sleep(bp.sleep_for(capture.backpressure().value))


# ═══════════════════════════════════════════════════════════════════════
# Main runner
# ═══════════════════════════════════════════════════════════════════════
async def _run(mode: str, tier: Optional[Tier], dry_run: bool = False) -> None:
    device = classify()
    print(user_facing_summary(device))

    profile = select_profile(tier)
    profile = apply_env_overrides(profile)
    budget = budget_for(profile.tier)
    logger.info(profile_summary(profile))

    if profile.tier == Tier.LOW and mode in ("standalone", "serve-ipc"):
        logger.error("low_tier_python_disabled — use --mode browser-only")
        raise SystemExit(2)

    if device.tier == DeviceTier.LIGHT:
        profile = replace(profile, model_size="tiny")
    elif device.tier in (DeviceTier.PERFORMANCE, DeviceTier.WORKSTATION):
        if device.gpu_kind != "none":
            profile = replace(profile, model_size="base")

    try:
        require_feature(
            "python_gpu_tiny" if device.gpu_kind != "none" else "python_cpu_tiny",
            device,
        )
    except FeatureUnavailableError as e:
        logger.error("feature_unavailable: %s", e.user_message)
        print(e.user_message)
        raise SystemExit(6)

    try:
        from core import guards
        if guards.pressure() == "critical":
            logger.warning(
                "thermal_critical: reducing model tier, waiting for device to cool"
            )
            profile = replace(profile, model_size="tiny")
    except ImportError:
        pass

    if mode == "browser-only":
        print("=== AXELR LOW Tier (Browser-only) ===")
        print("Serve JS bundle + /models/whisper.wasm + /models/whisper-tiny.bin")
        return

    try:
        adapter = select_adapter(ASRConfig(model_size=profile.model_size))
    except RuntimeError as e:
        logger.error("no_asr_adapter_available: %s", e)
        raise SystemExit(3)

    if adapter.name == "mock":
        if os.environ.get("AXELR_ALLOW_MOCK") != "1":
            raise SystemExit(3)
        logger.warning("*** RUNNING WITH MOCK ASR ***")

    print_health_report(profile, budget, adapter.name)
    if dry_run:
        logger.info("dry_run_complete")
        return

    local_fn = make_local_fn(adapter)
    cold_start = ColdStartProfiler({
        "ring_setup": 100.0, "adapter_load": 15000.0,
        "warm_up": 1000.0, "permissions": 200.0,
    })
    metrics = MetricsCollector()

    t0 = metrics.now()
    permissions = ZeroTrustPermissionManager()
    for p in (PermissionType.MICROPHONE, PermissionType.ACCESSIBILITY, PermissionType.CLIPBOARD):
        if not permissions.request(p):
            logger.warning("permission_not_granted: %s", p.value)
    cold_start.mark("permissions", metrics.elapsed_ms(t0))

    t0 = metrics.now()
    ring = ZeroCopyRingBuffer(RingBufferConfig(
        capacity_samples=profile.ring_capacity_samples,
        sample_rate=profile.sample_rate,
    ))
    cold_start.mark("ring_setup", metrics.elapsed_ms(t0))

    mic = MicrophoneSource(ring)

    pipeline_cfg = PipelineConfig(
        decoder=DecoderConfig(
            cloud_fallback_url=None,
            api_key=None,
            model_quantization=profile.quantization.value,
            max_local_memory_mb=profile.max_memory_mb,
        ),
        cloud=CloudConfig(),
        enable_structured_repair=profile.flags.structured_repair,
        enable_rerank=profile.flags.streaming_rerank,
    )
    pipeline = EnginePipeline(profile, pipeline_cfg, local_fn)

    pressure = MemoryPressureMonitor(
        PressureConfig(budget_mb=budget.budget_mb, high_watermark=0.85, low_watermark=0.65),
        on_high=pipeline.degradation.on_high, on_low=pipeline.degradation.on_low)
    pressure.start()

    hotkeys = HotkeyInterceptor()
    stop_event = asyncio.Event()
    hotkeys.register(Hotkey(0xBB, MOD_CONTROL | MOD_SHIFT, "start_recording"),
                     lambda: logger.info("start_hotkey"))
    hotkeys.start()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    t0 = metrics.now()
    await pipeline.start()
    mic.start()
    cold_start.mark("warm_up", metrics.elapsed_ms(t0))

    drain = asyncio.create_task(_drain_loop(mic, pipeline, metrics, stop_event))
    try:
        await stop_event.wait()
    finally:
        pressure.stop()
        drain.cancel()
        try: await drain
        except asyncio.CancelledError: pass
        await pipeline.stop()
        mic.stop()
        try: adapter.dispose()
        except Exception: logger.exception("adapter_dispose_failed")
        hotkeys.stop()
        ring.dispose()
        metrics.dump(logger)


def main() -> None:
    parser = argparse.ArgumentParser(description="Axelr Sovereign Engine")
    parser.add_argument(
        "--mode",
        choices=["standalone", "serve-ipc", "both", "browser-only", "preflight"],
        default="standalone",
    )
    parser.add_argument("--tier", choices=[t.value for t in Tier], default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args()

    if args.config:
        cfg = load_config_file(args.config)
        for k, v in cfg.items():
            if k.upper().startswith("AXELR_") and isinstance(v, str):
                os.environ[k.upper()] = v

    tier = Tier(args.tier) if args.tier else None
    try:
        if args.mode == "preflight":
            check = PreflightChecker().run(tier)
            print(check.summary())
            sys.exit(0 if check.ok else 1)
        asyncio.run(_run(args.mode, tier, dry_run=args.dry_run))
    except KeyboardInterrupt:
        logger.info("interrupted")
    except SystemExit:
        raise
    except Exception:
        logger.exception("fatal_error")
        raise


if __name__ == "__main__":
    main()
