import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core import resilience


@pytest.fixture
def mock_cfg():
    with patch("core.resilience._cfg") as mock:
        mock.cb_threshold = 5
        mock.cb_cooldown_s = 30
        mock.cb_rate_limit_cooldown_s = 60
        yield mock


@pytest.fixture
def circuit_breaker(mock_cfg):
    return resilience.CircuitBreaker()


class TestCircuitBreakerBasic:
    @pytest.mark.asyncio
    async def test_circuit_breaker_starts_closed(self, circuit_breaker):
        assert await circuit_breaker.is_available("test_scope") is True

    @pytest.mark.asyncio
    async def test_record_success_keeps_closed(self, circuit_breaker):
        await circuit_breaker.record_success("test_scope")
        assert await circuit_breaker.is_available("test_scope") is True

    @pytest.mark.asyncio
    async def test_single_failure_does_not_open(self, circuit_breaker):
        for _ in range(4):
            await circuit_breaker.record_failure("test_scope")
        assert await circuit_breaker.is_available("test_scope") is True

    @pytest.mark.asyncio
    async def test_threshold_failure_opens_circuit(self, circuit_breaker):
        for _ in range(5):
            await circuit_breaker.record_failure("test_scope")
        assert await circuit_breaker.is_available("test_scope") is False

    @pytest.mark.asyncio
    async def test_success_resets_circuit(self, circuit_breaker):
        for _ in range(5):
            await circuit_breaker.record_failure("test_scope")
        assert await circuit_breaker.is_available("test_scope") is False
        await circuit_breaker.record_success("test_scope")
        assert await circuit_breaker.is_available("test_scope") is True

    @pytest.mark.asyncio
    async def test_rate_limit_failure_opens_immediately(self, circuit_breaker):
        await circuit_breaker.record_failure("test_scope", is_rate_limit=True)
        assert await circuit_breaker.is_available("test_scope") is False

    @pytest.mark.asyncio
    async def test_circuit_breaker_cooldown_period(self, mock_cfg):
        mock_cfg.cb_cooldown_s = 0.1
        cb = resilience.CircuitBreaker()
        for _ in range(5):
            await cb.record_failure("test_scope")
        assert await cb.is_available("test_scope") is False
        await asyncio.sleep(0.15)
        assert await cb.is_available("test_scope") is True

    @pytest.mark.asyncio
    async def test_different_scopes_are_isolated(self, circuit_breaker):
        for _ in range(5):
            await circuit_breaker.record_failure("scope1")
        assert await circuit_breaker.is_available("scope1") is False
        assert await circuit_breaker.is_available("scope2") is True

    @pytest.mark.asyncio
    async def test_failure_window_expires(self, mock_cfg):
        mock_cfg.cb_cooldown_s = 0.5
        cb = resilience.CircuitBreaker()
        for _ in range(3):
            await cb.record_failure("test_scope")
        await asyncio.sleep(0.6)
        for _ in range(3):
            await cb.record_failure("test_scope")
        assert await cb.is_available("test_scope") is True

    @pytest.mark.asyncio
    async def test_redis_fallback_works(self, circuit_breaker):
        mock_redis = AsyncMock()
        mock_redis.get.return_value = None
        cb = resilience.CircuitBreaker(redis=mock_redis)
        assert await cb.is_available("test_scope") is True

    @pytest.mark.asyncio
    async def test_redis_open_circuit_detected(self, circuit_breaker):
        mock_redis = AsyncMock()
        mock_redis.get.return_value = b"1"
        cb = resilience.CircuitBreaker(redis=mock_redis)
        assert await cb.is_available("test_scope") is False

    @pytest.mark.asyncio
    async def test_record_failure_redis_pipeline(self, circuit_breaker):
        mock_redis = AsyncMock()
        mock_pipeline = AsyncMock()
        mock_pipeline.execute.return_value = [5]
        mock_redis.pipeline.return_value.__aenter__.return_value = mock_pipeline
        cb = resilience.CircuitBreaker(redis=mock_redis)
        await cb.record_failure("test_scope")
        assert mock_pipeline.incr.called
        assert mock_pipeline.expire.called

    @pytest.mark.asyncio
    async def test_record_success_redis_delete(self, circuit_breaker):
        mock_redis = AsyncMock()
        cb = resilience.CircuitBreaker(redis=mock_redis)
        await cb.record_success("test_scope")
        assert mock_redis.delete.called

    @pytest.mark.asyncio
    async def test_concurrent_access_thread_safety(self, circuit_breaker):
        async def record_many(scope):
            for _ in range(10):
                await circuit_breaker.record_failure(scope)
                await asyncio.sleep(0.001)
        await asyncio.gather(*[record_many(f"scope{i}") for i in range(5)])
        assert True

    @pytest.mark.asyncio
    async def test_circuit_breaker_global_instance(self):
        assert hasattr(resilience, "BREAKER")
        assert isinstance(resilience.BREAKER, resilience.CircuitBreaker)

    @pytest.mark.asyncio
    async def test_get_breaker_returns_singleton(self):
        b1 = resilience.get_breaker()
        b2 = resilience.get_breaker()
        assert b1 is b2


class TestFailoverTier:
    def test_failover_tier_enum_values(self):
        assert hasattr(resilience.FailoverTier, "FULL")
        assert hasattr(resilience.FailoverTier, "STANDARD")
        assert hasattr(resilience.FailoverTier, "STUB")
        assert hasattr(resilience.FailoverTier, "OFFLINE")

    def test_tiers_order(self):
        assert resilience.TIERS == [
            resilience.FailoverTier.FULL,
            resilience.FailoverTier.STANDARD,
            resilience.FailoverTier.STUB,
            resilience.FailoverTier.OFFLINE,
        ]

    @pytest.mark.asyncio
    async def test_ladder_run_succeeds_on_first_tier(self):
        def full_tier():
            return "success"
        result = await resilience.ladder_run([full_tier], [])
        assert result == "success"

    @pytest.mark.asyncio
    async def test_ladder_run_falls_back_on_failure(self):
        def full_tier():
            raise ValueError("fail")
        def stub_tier():
            return "fallback"
        result = await resilience.ladder_run([full_tier, stub_tier], [])
        assert result == "fallback"

    @pytest.mark.asyncio
    async def test_ladder_run_async_functions(self):
        async def full_tier():
            await asyncio.sleep(0.01)
            return "async success"
        result = await resilience.ladder_run([full_tier], [])
        assert result == "async success"

    @pytest.mark.asyncio
    async def test_ladder_run_all_fails_raises_last(self):
        def fail1():
            raise ValueError("error1")
        def fail2():
            raise ValueError("error2")
        with pytest.raises(ValueError, match="error2"):
            await resilience.ladder_run([fail1, fail2], [])

    @pytest.mark.asyncio
    async def test_ladder_run_passes_args(self):
        def add(a, b):
            return a + b
        result = await resilience.ladder_run([add], [2, 3])
        assert result == 5

    @pytest.mark.asyncio
    async def test_ladder_run_passes_kwargs(self):
        def multiply(a, b=2):
            return a * b
        result = await resilience.ladder_run([multiply], [3], {"b": 4})
        assert result == 12


class TestHotSwapDetector:
    def test_hot_swap_result_has_required_fields(self):
        assert hasattr(resilience.HotSwapResult, "winner")
        assert hasattr(resilience.HotSwapResult, "latency")
        assert hasattr(resilience.HotSwapResult, "confidence")

    def test_hot_swap_detector_initializes(self):
        detector = resilience.HotSwapDetector()
        assert detector is not None

    def test_hot_swap_detector_global_instance(self):
        assert hasattr(resilience, "get_hot_swap_detector")
        detector = resilience.get_hot_swap_detector()
        assert detector is not None

    @pytest.mark.asyncio
    async def test_hot_swap_detector_health_check(self):
        detector = resilience.HotSwapDetector()
        assert hasattr(detector, "check_health")

    @pytest.mark.asyncio
    async def test_hot_swap_detector_ewma_tracking(self):
        detector = resilience.HotSwapDetector()
        assert hasattr(detector, "record_latency")


class TestWaveRacer:
    def test_wave_racer_initializes(self):
        racer = resilience.WaveRacer(window_size=100)
        assert racer is not None

    def test_wave_racer_global_instance(self):
        assert hasattr(resilience, "RACER")
        assert resilience.RACER is not None

    def test_wave_racer_records_latency(self):
        racer = resilience.WaveRacer()
        racer.record(0.1)
        racer.record(0.2)
        assert True

    def test_wave_racer_p90_calculation(self):
        racer = resilience.WaveRacer(window_size=100)
        for i in range(100):
            racer.record(i * 0.01)
        p90 = racer.p90()
        assert 0.8 <= p90 <= 0.9

    def test_wave_racer_p50_calculation(self):
        racer = resilience.WaveRacer(window_size=100)
        for i in range(100):
            racer.record(i * 0.01)
        p50 = racer.p50()
        assert 0.45 <= p50 <= 0.55

    def test_wave_racer_p99_calculation(self):
        racer = resilience.WaveRacer(window_size=100)
        for i in range(100):
            racer.record(i * 0.01)
        p99 = racer.p99()
        assert 0.95 <= p99 <= 1.0

    def test_wave_racer_stats_shape(self):
        racer = resilience.WaveRacer()
        racer.record(0.1)
        stats = racer.stats()
        assert "p50" in stats
        assert "p90" in stats
        assert "p99" in stats
        assert "count" in stats
        assert "mean" in stats

    def test_wave_racer_window_eviction(self):
        racer = resilience.WaveRacer(window_size=10)
        for i in range(20):
            racer.record(float(i))
        stats = racer.stats()
        assert stats["count"] == 10

    def test_wave_racer_empty_window_returns_defaults(self):
        racer = resilience.WaveRacer()
        p50 = racer.p50()
        p90 = racer.p90()
        assert p50 == 0.0
        assert p90 == 0.0

    def test_wave_racer_single_value(self):
        racer = resilience.WaveRacer()
        racer.record(0.5)
        assert racer.p50() == 0.5
        assert racer.p90() == 0.5

    def test_wave_racer_concurrent_recording(self):
        racer = resilience.WaveRacer()
        import threading
        def record_many():
            for i in range(100):
                racer.record(float(i))
        threads = [threading.Thread(target=record_many) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert True

    def test_wave_racer_reset(self):
        racer = resilience.WaveRacer()
        racer.record(0.1)
        racer.record(0.2)
        racer.reset()
        stats = racer.stats()
        assert stats["count"] == 0


class TestResilienceModuleConstants:
    def test_cognition_level_exists(self):
        assert hasattr(resilience, "AXELR_COGNITION_LEVEL")
        assert isinstance(resilience.AXELR_COGNITION_LEVEL, str)

    def test_exported_symbols(self):
        required = [
            "CircuitBreaker",
            "BREAKER",
            "FailoverTier",
            "TIERS",
            "ladder_run",
            "HotSwapDetector",
            "WaveRacer",
            "RACER",
        ]
        for sym in required:
            assert sym in resilience.__all__

    def test_circuit_breaker_slots(self):
        cb = resilience.CircuitBreaker()
        assert hasattr(cb, "_redis")
        assert hasattr(cb, "_lock")
        assert hasattr(cb, "_states")

    def test_wave_racer_slots(self):
        racer = resilience.WaveRacer()
        assert hasattr(racer, "_window")
        assert hasattr(racer, "_max_size")