"""Master adversarial suite for Insights Engine — security, containment, protocol.

Rules for this file:
  * every test is marked `slow` — excluded from fast PR runs
"""
from __future__ import annotations

import json
import os
import pathlib
import socket

import pytest

pytestmark = [pytest.mark.slow, pytest.mark.adversarial]


# ══════════════════════════════════════════════════════════════════════════
# A. Query Shield — must block dangerous queries
# ══════════════════════════════════════════════════════════════════════════

DANGEROUS_KEYWORDS = ["DROP", "DELETE", "TRUNCATE", "ALTER", "GRANT", "REVOKE", "EXECUTE", "SYSTEM", "OS", "FILE"]

@pytest.mark.asyncio
async def test_a1_dangerous_query_blocked(insights_engine):
    """Queries with dangerous keywords must be blocked."""
    for kw in DANGEROUS_KEYWORDS:
        query = f"{kw} TABLE test_table"
        with pytest.raises(Exception):
            await insights_engine.execute_query(query)


# ══════════════════════════════════════════════════════════════════════════
# B. Data Containment — must not leak outside allowed tables
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_b1_unauthorized_table_access_blocked(insights_engine):
    """Queries to tables outside the allowed list must be blocked."""
    with pytest.raises(Exception):
        await insights_engine.execute_query("SELECT * FROM users")


@pytest.mark.asyncio
async def test_b2_sql_injection_blocked(insights_engine):
    """SQL injection attempts must be blocked."""
    injection_attempts = [
        "' OR '1'='1",
        "'; DROP TABLE orders; --",
        "' UNION SELECT password FROM users --",
        "' AND 1=1 --",
    ]
    for injection in injection_attempts:
        with pytest.raises(Exception):
            await insights_engine.execute_query(f"SELECT * FROM orders WHERE id = '{injection}'")


# ══════════════════════════════════════════════════════════════════════════
# C. Resource Limits — must enforce query timeouts and result caps
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_c1_query_timeout_enforced(insights_engine):
    """Long-running queries must be killed within timeout."""
    with pytest.raises(Exception):
        await insights_engine.execute_query("SELECT pg_sleep(10)")


@pytest.mark.asyncio
async def test_c2_result_size_capped(insights_engine):
    """Query results must not exceed maximum size limit."""
    result = await insights_engine.execute_query("SELECT * FROM large_table")
    assert len(result) <= 1000


# ══════════════════════════════════════════════════════════════════════════
# D. WAL Integrity — must maintain atomicity of query logs
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_d1_wal_atomicity_on_failure(insights_engine):
    """WAL must remain consistent if query fails mid-execution."""
    initial_count = len(insights_engine.wal.entries)
    try:
        await insights_engine.execute_query("INVALID QUERY THAT WILL FAIL")
    except Exception:
        pass
    assert len(insights_engine.wal.entries) == initial_count


@pytest.mark.asyncio
async def test_d2_wal_contains_all_successful_queries(insights_engine):
    """All successful queries must be logged to WAL."""
    initial_count = len(insights_engine.wal.entries)
    await insights_engine.execute_query("SELECT 1")
    assert len(insights_engine.wal.entries) == initial_count + 1


# ══════════════════════════════════════════════════════════════════════════
# E. Protocol Conformance — JSON-RPC 2.0 must be strictly enforced
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_e1_rpc_invalid_json_rejected(insights_engine):
    """Non-JRPC requests must be rejected."""
    from insights_agentic.actions import dispatch
    r = await dispatch(insights_engine, {"invalid": "request"})
    assert r["ok"] is False


@pytest.mark.asyncio
async def test_e2_rpc_missing_method_rejected(insights_engine):
    """JRPC requests without method must be rejected."""
    from insights_agentic.actions import dispatch
    r = await dispatch(insights_engine, {"jsonrpc": "2.0", "id": 1})
    assert r["ok"] is False


@pytest.mark.asyncio
async def test_e3_rpc_version_validation(insights_engine):
    """JRPC must require jsonrpc version 2.0."""
    from insights_agentic.actions import dispatch
    r = await dispatch(insights_engine, {"jsonrpc": "1.0", "method": "query", "params": {}, "id": 1})
    assert r["ok"] is False