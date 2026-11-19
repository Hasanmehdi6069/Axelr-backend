"""Specialized retry wrapper for inference endpoints - longer timeouts for long-running jobs"""
import asyncio
import logging
import random
from functools import wraps
from typing import Callable, TypeVar

logger = logging.getLogger("axelr.nebius.retry")

T = TypeVar('T')

# Inference-optimized exponential backoff with longer delays
def inference_retry_wrapper(
    max_retries: int = 7,  # More retries for long-running inference jobs
    base_delay: float = 2.0,  # Longer initial delay for inference
    max_delay: float = 120.0,  # 2 minute max delay for large model inference
    jitter: bool = True,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Specialized retry wrapper for model inference endpoints that handles:
    - Longer timeouts for large model inference
    - More retry attempts for transient cloud issues
    - Exponential backoff with jitter to prevent thundering herd
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            last_exception: Exception | None = None

            for attempt in range(max_retries + 1):
                try:
                    if attempt > 0:
                        # Calculate exponential backoff
                        delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                        if jitter:
                            delay *= random.uniform(0.75, 1.25)  # Narrower jitter for inference

                        logger.warning(
                            f"Inference retry {attempt}/{max_retries} for {func.__name__}, "
                            f"waiting {delay:.1f}s. Large model inference can take time..."
                        )
                        await asyncio.sleep(delay)

                    # Execute the inference call
                    result = await func(*args, **kwargs) if asyncio.iscoroutinefunction(func) else func(*args, **kwargs)

                    if attempt > 0:
                        logger.info(f"Inference {func.__name__} succeeded on attempt {attempt + 1}")
                    return result

                except (ConnectionError, TimeoutError, asyncio.TimeoutError) as e:
                    last_exception = e
                    logger.error(
                        f"Inference attempt {attempt + 1}/{max_retries + 1} failed: {str(e)}. "
                        f"This is common for large models on first load."
                    )
                except Exception as e:
                    # Don't retry on non-network errors
                    logger.critical(f"Non-retryable inference error: {str(e)}")
                    raise

            # All retries exhausted
            logger.critical(f"All {max_retries + 1} inference attempts failed")
            if last_exception:
                raise last_exception
            raise RuntimeError(f"All inference retries exhausted for {func.__name__}")

        return wrapper
    return decorator
