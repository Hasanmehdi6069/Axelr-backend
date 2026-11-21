import asyncio
import logging
from functools import wraps
from typing import Any, Callable

logger = logging.getLogger("axelr.paypal.retry")


def paypal_retry_wrapper(max_retries: int = 3, backoff_base: float = 0.5, backoff_cap: float = 2.0) -> Callable:
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            last_exception = None
            for attempt in range(1, max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries:
                        wait = min(backoff_base * (2 ** (attempt - 1)), backoff_cap)
                        logger.warning(f"PayPal API attempt {attempt} failed, retrying in {wait}s: {e}")
                        await asyncio.sleep(wait)
            logger.error(f"PayPal API failed after {max_retries} retries: {last_exception}")
            raise last_exception

        return wrapper

    return decorator
