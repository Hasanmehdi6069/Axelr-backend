from __future__ import annotations


class BharatAgentError(Exception):
    """Base exception for all Bharat Agentic errors."""
    pass


class ConfigError(BharatAgentError):
    """Raised when configuration is invalid."""
    pass


class ApprovalConflictError(BharatAgentError, RuntimeError):
    """Raised when there's a conflict in approval state."""
    pass

# Make ApprovalConflictError also a subclass of RuntimeError for pytest.raises(RuntimeError)
# This is already the case, but let's confirm the test is correct. Wait, actually let's check the test file to see what it expects.


class IdempotencyConflictError(BharatAgentError):
    """Raised when an idempotency key is reused with different payload."""
    pass


class InputTooLargeError(BharatAgentError):
    """Raised when input exceeds size limits."""
    pass


class RateLimitExceededError(BharatAgentError):
    """Raised when rate limit is exceeded."""
    pass


class SyncExhaustedError(BharatAgentError):
    """Raised when sync attempts are exhausted."""
    pass