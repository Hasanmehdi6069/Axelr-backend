"""
PII Vault for ephemeral personal identifiable information storage
Production-grade PII handling with automatic stale eviction and thread safety
"""
from __future__ import annotations

import asyncio
import time
from typing import Dict, Optional

import structlog

logger = structlog.get_logger("axelr.pii_vault")

# Constants
_PII_VAULT_TTL_S: int = 600  # 10 minute TTL for PII data
_EVICTION_INTERVAL_S: float = 60.0  # Run cleanup every minute
_PII_EVICTION_TASK: Optional[asyncio.Task] = None

# Vault state (thread-safe, protected by async locks)
_pii_sanitizer_vault: Dict[str, Dict[str, str]] = {}
_pii_vault_seen: Dict[str, float] = {}
_vault_lock: asyncio.Lock = asyncio.Lock()


def _evict_stale_pii_vault() -> None:
    """Synchronous stale PII cleanup (runs from background task)"""
    now = time.time()
    stale_keys = [k for k, ts in _pii_vault_seen.items() if now - ts > _PII_VAULT_TTL_S]
    
    if stale_keys:
        logger.debug("pii_vault_eviction count=%d", len(stale_keys))
        for key in stale_keys:
            _pii_vault_seen.pop(key, None)
            _pii_sanitizer_vault.pop(key, None)


async def async_evict_stale_pii_vault() -> None:
    """Thread-safe asynchronous stale PII cleanup"""
    async with _vault_lock:
        _evict_stale_pii_vault()


async def _pii_eviction_loop() -> None:
    """Background loop that automatically cleans up stale PII"""
    while True:
        try:
            await asyncio.sleep(_EVICTION_INTERVAL_S)
            await async_evict_stale_pii_vault()
        except asyncio.CancelledError:
            logger.info("pii_eviction_loop_cancelled")
            return
        except Exception as e:
            logger.warning("pii_eviction_error error=%s", str(e))
            await asyncio.sleep(5.0)  # Backoff before retry


def start_pii_eviction_task() -> None:
    """Start the background PII eviction task if not already running"""
    global _PII_EVICTION_TASK
    if _PII_EVICTION_TASK is None or _PII_EVICTION_TASK.done():
        _PII_EVICTION_TASK = asyncio.create_task(_pii_eviction_loop())
        logger.info("pii_eviction_task_started")


def stop_pii_eviction_task() -> None:
    """Stop the background PII eviction task"""
    global _PII_EVICTION_TASK
    if _PII_EVICTION_TASK and not _PII_EVICTION_TASK.done():
        _PII_EVICTION_TASK.cancel()
        _PII_EVICTION_TASK = None
        logger.info("pii_eviction_task_stopped")


async def mask_pii(
    session_id: str, 
    tenant_id: str, 
    text: str, 
    sensitive_values: list[str]
) -> str:
    """Thread-safe PII masking - replaces sensitive values with placeholders"""
    await async_evict_stale_pii_vault()  # Cleanup before insertion
    session_key = f"{session_id}:{tenant_id}"
    
    async with _vault_lock:
        _pii_vault_seen[session_key] = time.time()
        vault = _pii_sanitizer_vault.setdefault(session_key, {})
        masked = text
        
        for i, val in enumerate(sensitive_values):
            if val and val in masked:
                masked_id = f"[REDACTED_{i}]"
                vault[masked_id] = val
                masked = masked.replace(val, masked_id)
        
        return masked


async def restore_pii(session_id: str, tenant_id: str, text: str) -> str:
    """Thread-safe PII restoration - replaces placeholders with original values"""
    session_key = f"{session_id}:{tenant_id}"
    
    async with _vault_lock:
        vault = _pii_sanitizer_vault.get(session_key)
        if not vault:
            return text
            
        _pii_vault_seen[session_key] = time.time()  # Refresh timestamp
        restored = text
        
        for masked_id, original in vault.items():
            if masked_id in restored:
                restored = restored.replace(masked_id, original)
                
        return restored


def get_sanitizer_vault() -> Dict[str, Dict[str, str]]:
    """Read-only access to the PII vault for debugging/monitoring"""
    return _pii_sanitizer_vault.copy()


def clear_vault_for_session(session_id: str, tenant_id: str) -> None:
    """Explicitly clear PII data for a specific session"""
    session_key = f"{session_id}:{tenant_id}"
    _pii_vault_seen.pop(session_key, None)
    _pii_sanitizer_vault.pop(session_key, None)
    logger.debug("pii_vault_cleared session=%s tenant=%s", session_id, tenant_id)