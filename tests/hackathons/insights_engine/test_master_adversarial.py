"""Master adversarial suite for Insights Engine — security, containment, protocol.

Rules for this file:
  * every test is marked `slow` — excluded from fast PR runs
  * every test is adversarial — it tries to break the engine
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.slow, pytest.mark.adversarial]


# ══════════════════════════════════════════════════════════════════════════
# A. Query Shield — must block dangerous SQL
# ══════════════════════════════════════════════════════════════════════════

DANGEROUS_KEYWORDS = [
    "DROP TABLE",
    "DELETE FROM",
    "TRUNCATE",
    "ALTER TABLE",
    "CREATE TABLE",
    "INSERT INTO",
    "UPDATE SET",
    "GRANT",
    "REVOKE",
    "EXECUTE",
    "EXEC ",
    "sp_",
    "xp_cmdshell",
    "--",
    ";",
    "/*",
    "*/",
    "UNION SELECT",
    "OR 1=1",
]


@pytest.mark.asyncio
async def test_a1_dangerous_sql_blocked(insights_engine):
    """Dangerous SQL keywords must be blocked."""
    for keyword in DANGEROUS_KEYWORDS:
        with pytest.raises(Exception) as excinfo:
            await insights_engine.execute_query(f"SELECT * FROM users WHERE id = 1 {keyword}")
        assert "blocked" in str(excinfo.value).lower() or "invalid" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_a2_union_injection_blocked(insights_engine):
    """UNION injection attempts must be blocked."""
    with pytest.raises(Exception):
        await insights_engine.execute_query("SELECT * FROM users UNION SELECT * FROM passwords")


@pytest.mark.asyncio
async def test_a3_boolean_bypass_blocked(insights_engine):
    """Boolean logic bypass attempts must be blocked."""
    with pytest.raises(Exception):
        await insights_engine.execute_query("SELECT * FROM users WHERE username = 'admin' OR '1'='1'")


# ══════════════════════════════════════════════════════════════════════════
# B. Resource Containment — must enforce query limits
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_b1_query_timeout_enforced(insights_engine):
    """Long-running queries must be terminated by timeout."""
    with pytest.raises(TimeoutError):
        await insights_engine.execute_query("SELECT SLEEP(10)", timeout=1)


@pytest.mark.asyncio
async def test_b2_result_size_capped(insights_engine):
    """Query results must not exceed maximum size limit."""
    large_query = "SELECT * FROM large_table"
    result = await insights_engine.execute_query(large_query, max_rows=1000)
    assert len(result) <= 1000, f"Result size exceeded: {len(result)} rows"


@pytest.mark.asyncio
async def test_b3_memory_limit_enforced(insights_engine):
    """Memory-intensive queries must be rejected."""
    memory_query = "SELECT * FROM huge_table JOIN huge_table JOIN huge_table"
    with pytest.raises(MemoryError):
        await insights_engine.execute_query(memory_query, max_memory_mb=10)


# ══════════════════════════════════════════════════════════════════════════
# C. Authorization — must enforce row-level security
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_c1_cross_tenant_access_blocked(insights_engine):
    """Users must not access other tenants' data."""
    with pytest.raises(Exception):
        await insights_engine.execute_query("SELECT * FROM tenant_data WHERE tenant_id = 2", user_id=1)


@pytest.mark.asyncio
async def test_c2_column_level_security_enforced(insights_engine):
    """Users must not access restricted columns."""
    result = await insights_engine.execute_query("SELECT * FROM users", user_role="viewer")
    assert "password_hash" not in result.columns, "Restricted column accessible"
    assert "credit_card" not in result.columns, "Restricted column accessible"


# ══════════════════════════════════════════════════════════════════════════
# D. Protocol Conformance — SQL must be strictly validated
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_d1_invalid_syntax_rejected(insights_engine):
    """Invalid SQL syntax must be rejected."""
    invalid_queries = [
        "SELECT * FROM",
        "FROM users WHERE",
        "SELECT * WHERE id =",
        "INVALID SYNTAX HERE",
    ]
    for query in invalid_queries:
        with pytest.raises(Exception):
            await insights_engine.execute_query(query)


@pytest.mark.asyncio
async def test_d2_unknown_table_rejected(insights_engine):
    """Queries on unknown tables must be rejected."""
    with pytest.raises(Exception):
        await insights_engine.execute_query("SELECT * FROM nonexistent_table_12345")


@pytest.mark.asyncio
async def test_d3_unknown_column_rejected(insights_engine):
    """Queries on unknown columns must be rejected."""
    with pytest.raises(Exception):
        await insights_engine.execute_query("SELECT nonexistent_column FROM users")
