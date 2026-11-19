"""Exponential backoff retry wrapper for all external API calls"""
import asyncio
import logging
import random
from functools import wraps
from typing import Callable, Optional, TypeVar

logger = logging.getLogger("axelr.retry")

T = TypeVar('T')

def with_exponential_backoff(
    max_retries: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    jitter: bool = True,
    retry_exceptions: tuple[type[Exception], ...] = (Exception,),
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Exponential backoff decorator with jitter for robust API retry logic.
    
    Args:
        max_retries: Maximum number of retry attempts
        base_delay: Initial delay in seconds between retries
        max_delay: Maximum delay cap (prevents extremely long waits)
        jitter: Add randomness to delay to avoid thundering herd
        retry_exceptions: Tuple of exception types that trigger retries
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            last_exception: Optional[Exception] = None

            for attempt in range(max_retries + 1):
                try:
                    if attempt > 0:
                        delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                        if jitter:
                            delay *= random.uniform(0.5, 1.5)

                        logger.warning(
                            f"Retry attempt {attempt}/{max_retries} for {func.__name__}, "
                            f"waiting {delay:.2f}s"
                        )
                        await asyncio.sleep(delay)

                    result = await func(*args, **kwargs) if asyncio.iscoroutinefunction(func) else func(*args, **kwargs)

                    if attempt > 0:
                        logger.info(f"{func.__name__} succeeded on attempt {attempt + 1}")
                    return result

                except retry_exceptions as e:
                    last_exception = e
                    logger.error(
                        f"Attempt {attempt + 1}/{max_retries + 1} failed for {func.__name__}: {str(e)}"
                    )

            # If we exhaust all retries
            logger.critical(f"All {max_retries + 1} attempts failed for {func.__name__}")
            if last_exception:
                raise last_exception
            raise RuntimeError(f"All retries exhausted for {func.__name__}")

        return wrapper
    return decorator


def create_retry_wrapper(adapter_name: str):
    """Create a pre-configured retry wrapper for a specific adapter"""
    return with_exponential_backoff(
        max_retries=5,
        base_delay=1.0,
        max_delay=30.0,
        jitter=True,
        retry_exceptions=(ConnectionError, TimeoutError, asyncio.TimeoutError)
    )
