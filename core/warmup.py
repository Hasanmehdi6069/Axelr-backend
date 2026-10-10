# core/warmup.py
"""
AXELR — Predictive Driver Wakeup & Cold-Start Pacemaker (F44)
================================================================
A pre-submit hint endpoint sets a short-lived key. A background task
in the lifespan polls the key and pre-warms the DB clients a few
hundred milliseconds before the user hits submit, so the actual
request does not pay the driver cold-start.

Zero additional RAM in the API container: the pre-warm just pings the
already-open driver; if the driver has not been opened yet, warmup is
a no-op.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time

logger = logging.getLogger("axelr.warmup")

_WARMUP_KEY = "axelr:warmup:signal"
_WARMUP_TTL_S = 5


async def signal_warmup(redis_client) -> bool:
    """Record that a submit is imminent. Best-effort; never raises."""
    if redis_client is None:
        return False
    with contextlib.suppress(Exception):
        await redis_client.setex(_WARMUP_KEY, _WARMUP_TTL_S, str(time.time()))
        return True
    return False


async def pacemaker_loop(state, *, interval_s: float = 0.5) -> None:
    """
    Long-running watcher. Called from lifespan as a background task.
    Pings the DB and Redis drivers when the warmup key is live.
    """
    while True:
        try:
            await asyncio.sleep(interval_s)
            r = getattr(state, "redis_client", None)
            if r is None:
                continue
            with contextlib.suppress(Exception):
                if await r.get(_WARMUP_KEY) is None:
                    continue
            # Warmup key live — ping both drivers.
            if getattr(state, "db_available", False) and getattr(state, "db", None):
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(state.db.command("ping"), timeout=0.5)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(r.ping(), timeout=0.3)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.debug("pacemaker_loop_error error=%s", e)


__all__ = ["signal_warmup", "pacemaker_loop"]