# core/_compat.py
"""
AXELR — Backward-compat shim for v6 → v7 consolidation
========================================================
Temporary. Every symbol here lives canonically in a merged module. This
file re-exports them so existing call sites (tests, api/, external code)
import without change.

Scheduled for removal in v8.0. Do not add new symbols here.
"""

from __future__ import annotations

try:
    from .compliance import (
        clear_vault_for_session,
        mask_pii,
        restore_pii,
        start_pii_eviction_task,
        stop_pii_eviction_task,
    )
except ImportError:

    async def mask_pii(session_id, tenant_id, text, sensitive_values):
        return text

    async def restore_pii(session_id, tenant_id, text):
        return text

    def start_pii_eviction_task():
        pass

    def stop_pii_eviction_task():
        pass

    def clear_vault_for_session(session_id, tenant_id):
        pass


__all__ = [n for n in dir() if not n.startswith("_")]
