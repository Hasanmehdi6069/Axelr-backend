"""Engine tests — fast, dependency-light, honest.

Ollama-dependent tests skip automatically when Apex Router is unreachable.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
from axelr.apex_client import (
    ApexRouterClient,
    CloudCallForbidden,
    _is_local_host,
    normalize_metadata,
)
from axelr.core import (
    DailySummary,
    DocumentMetadata,
    ProcessingResult,
    UndoEntry,
    UndoLog,
    _safe_segment,
    _target_path,
)


class TestLocalHostGuard:
    @pytest.mark.parametrize(
        "host",
        [
            "127.0.0.1",
            "127.1.2.3",
            "localhost",
            "10.0.0.5",
            "192.168.1.1",
            "172.16.0.1",
            "::1",
        ],
    )
    def test_local_accepted(self, host: str) -> None:
        assert _is_local_host(host) is True

    @pytest.mark.parametrize("host", ["8.8.8.8", "1.1.1.1", ""])
    def test_public_rejected(self, host: str) -> None:
        assert _is_local_host(host) is False

    def test_client_raises_on_public_endpoint(self) -> None:
        with pytest.raises(CloudCallForbidden):
            ApexRouterClient(endpoint="http://8.8.8.8:11434")


class TestMetadataNormalization:
    def test_full_payload(self) -> None:
        out = normalize_metadata(
            {
                "document_type": "invoice",
                "date": "2026-11-03",
                "sender": "Acme Corp",
                "total_amount": 4200.00,
                "currency": "usd",
                "due_date": "2026-11-10",
                "category": "Invoices",
                "confidence": 0.92,
            }
        )
        assert out["document_type"] == "invoice"
        assert out["total_amount"] == 4200.0
        assert out["currency"] == "USD"  # uppercased

    def test_unknown_doc_type_falls_back(self) -> None:
        out = normalize_metadata({"document_type": "banana", "category": "Other", "confidence": 0.5})
        assert out["document_type"] == "other"

    def test_short_currency_nulled(self) -> None:
        out = normalize_metadata(
            {
                "document_type": "invoice",
                "category": "Invoices",
                "confidence": 0.5,
                "currency": "US",
            }
        )
        assert out["currency"] is None

    def test_confidence_clamped(self) -> None:
        out = normalize_metadata(
            {
                "document_type": "invoice",
                "category": "Invoices",
                "confidence": 1.5,
            }
        )
        assert out["confidence"] == 1.0


class TestSafeSegment:
    @pytest.mark.parametrize(
        "raw,want",
        [
            ("Acme Corp", "Acme-Corp"),
            ("../../etc/passwd", "etc-passwd"),
            ("", "untitled"),
            ("...", "untitled"),
        ],
    )
    def test_safe(self, raw: str, want: str) -> None:
        assert _safe_segment(raw) == want


class TestTargetPath:
    def test_layout(self, tmp_path: Path) -> None:
        meta = DocumentMetadata(
            document_type="invoice",
            date="2026-11-03",
            sender="Acme Corp",
            category="Invoices",
        )
        dst = _target_path(tmp_path, meta, Path("invoice-001.pdf"))
        assert dst.parent == tmp_path / "2026" / "11" / "Invoices"
        assert dst.name == "2026-11-03__Acme-Corp__invoice-001.pdf"


@pytest.mark.asyncio
async def test_undo_log_cap(tmp_path: Path) -> None:
    log = UndoLog(tmp_path / "undo.jsonl", max_entries=3)
    for i in range(5):
        await log.record(
            UndoEntry(
                op_id=str(i),
                timestamp=float(i),
                src=f"/a/{i}",
                dst=f"/b/{i}",
                filename=f"f{i}",
                category="Invoices",
                content_hash=f"h{i}",
            )
        )
    snap = await log.snapshot()
    assert [e.op_id for e in snap] == ["2", "3", "4"]


@pytest.mark.asyncio
async def test_daily_summary_roundtrip(tmp_path: Path) -> None:
    summary = DailySummary(tmp_path)
    await summary.append(
        ProcessingResult(
            source="/inbox/x.pdf",
            source_name="x.pdf",
            destination="/lib/2026/11/Invoices/x.pdf",
            metadata=DocumentMetadata(
                document_type="invoice",
                date="2026-11-03",
                sender="Acme",
                category="Invoices",
                confidence=0.9,
            ),
        )
    )
    text = await summary.render()
    assert "1 document" in text
    assert "Invoices: 1" in text


def _apex_available() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 11434), timeout=1):
            return True
    except OSError:
        return False


@pytest.mark.skipif(not _apex_available(), reason="Apex Router not running")
@pytest.mark.asyncio
async def test_end_to_end_file(tmp_path: Path) -> None:
    from axelr.core import DocumentSkill

    root = tmp_path / "nas"
    (root / "inbox").mkdir(parents=True)
    src = root / "inbox" / "invoice-001.txt"
    src.write_text(
        "INVOICE\nDate: 2026-11-03\nFrom: Acme Corp\nTotal: USD 4200.00\nDue: 2026-11-10\n",
        encoding="utf-8",
    )
    skill = await DocumentSkill.create(root)
    result = await skill.process_now(src)
    await skill.stop()

    assert result is not None
    assert result.error is None
    assert Path(result.destination).exists()
    assert not src.exists()
