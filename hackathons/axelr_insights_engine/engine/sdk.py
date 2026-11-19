"""axelr-insights-engine — SDK wrapper.

Single choke point for every Appwrite SDK call. Every public method
goes through `AppwriteSDK.call()`, which is where resilience lives:

  * Token bucket (rate limit)
  * Semaphore (concurrency cap)
  * Circuit breaker
  * Retry with backoff + jitter (retryable errors only)
  * Metrics observation
  * Structured logging
  * Request deduplication (for reads)

Auth modes (client-side):
  * anonymous:  no credentials — read-only public data
  * session:    user-scoped via session secret (browser / desktop)
  * server:     admin-scoped via API key

This module is transport-agnostic — it does not know about tables.
`tables.py` composes on top of it.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from appwrite.client import Client
from appwrite.services.tables_db import TablesDB

from .config import AppwriteConfig
from .errors import (
    CircuitBreakerOpenError,
    ClientError,
    TimeoutError_,
    is_retryable,
    normalize_error,
    wrap_sdk_error,
)
from .resilience import (
    CBState,
    CircuitBreaker,
    LocalCache,
    MetricsCollector,
    RequestDeduplicator,
    Semaphore,
    StructuredLogger,
    TokenBucket,
    generate_corr_id,
    jittered_sleep,
)


class AppwriteSDK:
    """Async wrapper around the sync Appwrite SDK with resilience."""

    def __init__(
        self,
        config: AppwriteConfig,
        *,
        client_factory: Callable[[], Any] | None = None,
        tables_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        self._config = config
        self._log = StructuredLogger(
            level=config.log_level,
            context={
                "component": "appwrite_sdk",
                "mode": config.mode,
                "project": config.project_id,
            },
        )
        self._metrics = MetricsCollector()
        self._cache = LocalCache(
            max_entries=config.cache_max_entries,
            ttl_s=config.cache_ttl_s,
        )
        self._breaker = CircuitBreaker(
            threshold=config.circuit_threshold,
            cooldown_s=config.circuit_cooldown_s,
        )
        self._bucket = TokenBucket(
            rate_per_sec=config.rate_limit_per_min / 60.0,
            capacity=config.rate_limit_burst,
        )
        self._semaphore = Semaphore(config.max_concurrent_requests)
        self._dedup = RequestDeduplicator()

        # Build the client
        self._client = self._build_client(config, client_factory)
        self._tables = (tables_factory or TablesDB)(self._client)

        self._log.info(
            "sdk_initialized",
            auth_kind=config.auth_kind,
            rate_limit_per_min=config.rate_limit_per_min,
            circuit_threshold=config.circuit_threshold,
        )

    # ── Client construction ──────────────────────────────────────────

    @staticmethod
    def _build_client(
        config: AppwriteConfig,
        factory: Callable[[], Any] | None,
    ) -> Any:
        client = factory() if factory else Client()
        client.set_endpoint(config.endpoint)
        client.set_project(config.project_id)
        if config.api_key:
            client.set_key(config.api_key)
        elif config.session_secret:
            client.set_session(config.session_secret)
        return client

    # ── Accessors ────────────────────────────────────────────────────

    @property
    def client(self) -> Any:
        return self._client

    @property
    def tables(self) -> Any:
        return self._tables

    @property
    def config(self) -> AppwriteConfig:
        return self._config

    @property
    def metrics(self) -> MetricsCollector:
        return self._metrics

    @property
    def cache(self) -> LocalCache:
        return self._cache

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    @property
    def log(self) -> StructuredLogger:
        return self._log

    def set_session(self, session_secret: str) -> None:
        """Swap the current session at runtime (e.g. after sign-in)."""
        self._client.set_session(session_secret)
        self._log.info("session_updated")

    # ── Core choke point ─────────────────────────────────────────────

    async def call(
        self,
        operation: str,
        fn: Callable[[], Any] | Callable[[], Awaitable[Any]],
        *,
        cache_parts: tuple[Any, ...] | None = None,
        cacheable: bool = False,
        dedup_key: str | None = None,
        timeout_s: float | None = None,
        bypass_circuit: bool = False,
    ) -> Any:
        """Execute `fn` with full resilience.

        `fn` may return an awaitable (async) or a plain value (sync SDK).
        Sync functions are run in a thread pool via `asyncio.to_thread`.
        """
        corr_id = generate_corr_id()
        log = self._log.with_(operation=operation, corr_id=corr_id)

        # Cache read
        if cacheable and cache_parts is not None:
            cached = self._cache.get(cache_parts)
            if cached is not None:
                self._metrics.inc("sdk.cache_hit")
                self._metrics.inc(f"sdk.{operation}.cache_hit")
                return cached

        async def _execute() -> Any:
            # Circuit gate
            if not bypass_circuit and not await self._breaker.allow():
                self._metrics.inc("sdk.circuit_reject")
                raise CircuitBreakerOpenError(f"circuit open for {operation}")

            # Rate limit gate
            await self._bucket.acquire()

            # Concurrency gate
            await self._semaphore.acquire()
            try:
                return await self._run_with_retry(
                    operation, fn, corr_id, log, timeout_s, bypass_circuit,
                )
            finally:
                self._semaphore.release()

        # Dedup
        if dedup_key is not None:
            result = await self._dedup.run(dedup_key, _execute)
        else:
            result = await _execute()

        # Cache write
        if cacheable and cache_parts is not None:
            self._cache.set(cache_parts, result)

        return result

    async def _run_with_retry(
        self,
        operation: str,
        fn: Callable[[], Any],
        corr_id: str,
        log: StructuredLogger,
        timeout_s: float | None,
        bypass_circuit: bool,
    ) -> Any:
        """Retry loop — the only place backoff lives."""
        timeout = timeout_s if timeout_s is not None else self._config.request_timeout_s
        last_exc: BaseException | None = None
        max_attempts = self._config.max_retries

        for attempt in range(1, max_attempts + 1):
            started = time.monotonic()
            try:
                result = await self._invoke_with_timeout(fn, timeout, operation)
                elapsed_ms = (time.monotonic() - started) * 1000.0
                self._metrics.observe(f"sdk.{operation}.duration_ms", elapsed_ms)
                self._metrics.inc(f"sdk.{operation}.success")
                if not bypass_circuit:
                    await self._breaker.record_success()
                log.info(
                    "sdk_call_ok",
                    attempt=attempt,
                    duration_ms=round(elapsed_ms, 2),
                )
                return result
            except asyncio.CancelledError:
                raise
            except BaseException as raw_err:
                err = normalize_error(raw_err)
                last_exc = err
                retryable = is_retryable(err)
                final_attempt = attempt >= max_attempts

                if not retryable or final_attempt:
                    if not bypass_circuit:
                        await self._breaker.record_failure()
                    self._metrics.inc(f"sdk.{operation}.failure")
                    log.error(
                        "sdk_call_failed",
                        attempt=attempt,
                        status=err.status,
                        code=err.code,
                        message=err.message,
                    )
                    raise wrap_sdk_error(
                        err, operation=operation, attempt=attempt, corr_id=corr_id,
                    ) from raw_err

                delay = self._policy_delay(attempt)
                self._metrics.inc(f"sdk.{operation}.retry")
                log.warn(
                    "sdk_call_retry",
                    attempt=attempt,
                    delay_s=round(delay, 3),
                    code=err.code,
                    message=err.message,
                )
                await jittered_sleep(delay, self._config.retry_jitter)

        if last_exc is not None:
            raise last_exc
        raise ClientError(f"operation '{operation}' exhausted retries")

    def _policy_delay(self, attempt: int) -> float:
        """Backoff for the Nth retry attempt (1 = first retry after first failure)."""
        exp = self._config.retry_base_delay_s * (2 ** (attempt - 1))
        return min(self._config.retry_max_delay_s, exp)

    async def _invoke_with_timeout(
        self,
        fn: Callable[[], Any],
        timeout_s: float,
        operation: str,
    ) -> Any:
        """Invoke `fn` (sync or async) with timeout."""
        if asyncio.iscoroutinefunction(fn):
            coro = fn()
        else:
            coro = asyncio.to_thread(fn)  # type: ignore[arg-type]
        if timeout_s <= 0:
            return await coro
        try:
            return await asyncio.wait_for(coro, timeout=timeout_s)
        except asyncio.TimeoutError as e:
            raise TimeoutError_(f"operation '{operation}' timed out after {timeout_s}s") from e

    # ── Convenience calls ────────────────────────────────────────────

    async def call_read(
        self,
        operation: str,
        fn: Callable[[], Any],
        cache_parts: tuple[Any, ...],
        *,
        timeout_s: float | None = None,
    ) -> Any:
        """Cacheable, deduped read."""
        return await self.call(
            operation, fn,
            cache_parts=cache_parts, cacheable=True,
            dedup_key=f"{operation}:{cache_parts}",
            timeout_s=timeout_s,
        )

    async def call_many(
        self,
        operation: str,
        calls: list[Callable[[], Any]],
        *,
        return_exceptions: bool = False,
    ) -> list[Any]:
        """Run N calls concurrently under the semaphore."""
        tasks = [self.call(operation, fn) for fn in calls]
        return await asyncio.gather(*tasks, return_exceptions=return_exceptions)

    # ── Health ───────────────────────────────────────────────────────

    async def health(self, *, deep: bool = False) -> dict[str, Any]:
        """Probe the SDK. Never raises."""
        report: dict[str, Any] = {
            "healthy": False,
            "backend": "appwrite",
            "mode": self._config.mode,
            "database_id": self._config.database_id,
            "circuit_state": self._breaker.state,
            "cache_size": self._cache.size,
            "auth_kind": self._config.auth_kind,
            "timestamp": time.time(),
        }
        if self._breaker.state == CBState.OPEN:
            report["reason"] = "circuit_open"
            return report
        try:
            await self.call(
                "health_probe",
                lambda: self._tables.list_tables(
                    database_id=self._config.database_id,
                ),
                timeout_s=5.0,
                bypass_circuit=True,
            )
            report["healthy"] = True
            if deep:
                report["metrics"] = self._metrics.snapshot()
        except Exception as e:
            report["reason"] = str(e)[:200]
        return report

    # ── Lifecycle ────────────────────────────────────────────────────

    async def close(self) -> None:
        await self._breaker.reset()
        self._cache.clear()
        self._dedup.clear()
        self._log.info("sdk_closed")

    def __repr__(self) -> str:
        return (
            f"AppwriteSDK(mode={self._config.mode!r}, "
            f"project={self._config.project_id!r}, "
            f"circuit={self._breaker.state!r})"
        )
