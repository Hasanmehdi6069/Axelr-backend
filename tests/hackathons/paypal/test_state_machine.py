"""Unit tests — offline, no credentials, no network.

Honest note: these verify our code's contract. Live PayPal behavior is
covered by `verification.py` against sandbox.
"""
from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.paypal.client import (
    _regex_fallback_extraction,
    extract_payment_intent,
    get_paypal_client,
)
from src.adapters.paypal.core import (
    get_audit_logs,
    get_core_module,
    log_audit,
)
from src.adapters.paypal.transactions import get_transaction_store
from src.adapters.paypal.schemas import (
    CreateOrderRequest,
    OrderItem,
    PaymentIntent,
    RefundRequest,
    TransactionState,
)
from src.adapters.paypal.webhooks import _LOCAL_IDEM, process_webhook_event

SAMPLE1 = "Invoice #1234: Due by 2024-12-31, $150.00 USD from John Doe john@example.com - web hosting services"
SAMPLE2 = "Monthly subscription for $29.99, recurring, bill to Jane Smith jane.smith@company.org"
SAMPLE3 = "Payout of 1000.00 EUR to Alex Johnson, pay by 2024-11-15 for contractor work"


@pytest.fixture(autouse=True)
def _clean():
    store = get_transaction_store()
    store.in_memory_store.clear()
    store._order_index.clear()
    store._capture_index.clear()
    store._processed_webhook_ids.clear()
    _LOCAL_IDEM.clear()
    get_core_module().settings.client_id = "test"
    get_core_module().settings.client_secret = "test"
    yield


# ── Mandatory contract tests ───────────────────────────────────────────────
def test_create_order_invalid_amount_rejected():
    with pytest.raises(ValueError, match="total amount must be a valid Decimal string"):
        CreateOrderRequest(amount="invalid", currency="USD", items=[])


def test_create_order_items_total_mismatch_rejected():
    with pytest.raises(ValueError, match="does not match order total"):
        CreateOrderRequest(
            amount="100.00", currency="USD",
            items=[
                OrderItem(name="A", quantity=1, unit_amount="50.00"),
                OrderItem(name="B", quantity=1, unit_amount="60.00"),
            ],
        )


def test_nan_amount_rejected():
    with pytest.raises(ValueError, match="finite"):
        CreateOrderRequest(amount="NaN", currency="USD", items=[])


@pytest.mark.asyncio
async def test_invalid_state_transition_rejected():
    store = get_transaction_store()
    txn = await store.create_transaction(amount="100.00", currency="USD", items=[])
    with pytest.raises(ValueError, match="invalid state transition from INITIATED to CAPTURED"):
        await store.transition_state(txn.id, TransactionState.CAPTURED)


@pytest.mark.asyncio
async def test_webhook_duplicate_event_processed_once():
    store = get_transaction_store()
    txn = await store.create_transaction(amount="100.00", currency="USD", items=[])
    await store.transition_state(txn.id, TransactionState.VALIDATED)
    await store.transition_state(txn.id, TransactionState.PAYPAL_CREATED)
    txn.paypal_order_id = "TEST-ORDER-123"
    await store._save(txn)
    await store.add_paypal_order_index("TEST-ORDER-123", txn.id)

    event = {
        "id": "evt-123456",
        "event_type": "CHECKOUT.ORDER.APPROVED",
        "resource": {"id": "TEST-ORDER-123"},
    }
    body = json.dumps(event).encode()
    with patch(
        "src.adapters.paypal.client.PayPalClient.verify_webhook_signature",
        new=AsyncMock(return_value=True),
    ):
        r1 = await process_webhook_event({"x": "y"}, body)
        assert r1["accepted"] is True
        after1 = await store.get_transaction(txn.id)
        assert after1.state == TransactionState.APPROVED

        r2 = await process_webhook_event({"x": "y"}, body)
        assert r2.get("duplicate") is True
        after2 = await store.get_transaction(txn.id)
        assert after2.state == TransactionState.APPROVED  # unchanged


def test_refund_without_confirm_rejected():
    with pytest.raises(ValueError, match="confirm must be True to process refund"):
        RefundRequest(
            capture_id="CAP-X", amount="50.00", currency="USD", confirm=False,
        )


@pytest.mark.asyncio
async def test_ai_extraction_returns_valid_json():
    with patch("src.adapters.paypal.client.get_core_module") as mock_core:
        inst = MagicMock()
        inst.settings.openai_api_key = None
        inst.settings.anthropic_api_key = None
        mock_core.return_value = inst
        for sample in (SAMPLE1, SAMPLE2, SAMPLE3):
            result = await extract_payment_intent(sample)
            assert isinstance(result, PaymentIntent)
            assert Decimal(result.amount) > 0
            assert len(result.currency) == 3
            assert len(result.items) >= 1
            assert result.type in {"invoice", "subscription", "payout"}
            assert 0.0 <= result.confidence <= 1.0


# ── Regression: state must be checked BEFORE money moves ─────────────────
@pytest.mark.asyncio
async def test_capture_rejected_before_money_moves():
    """If txn exists and state != APPROVED, PayPal must NOT be called."""
    from src.adapters.paypal.routes import capture_order, CaptureOrderRequest
    store = get_transaction_store()
    txn = await store.create_transaction(amount="1.00", currency="USD", items=[])
    await store.add_paypal_order_index("ORD-BAD", txn.id)  # INITIATED, not APPROVED
    with patch("src.adapters.paypal.routes.get_paypal_client") as mock_get:
        mock_client = MagicMock()
        mock_client.capture_order = AsyncMock()
        mock_get.return_value = mock_client
        req = CaptureOrderRequest(idempotency_key=None)
        with pytest.raises(Exception) as excinfo:
            await capture_order("ORD-BAD", req, {"_id": "u1"})
        assert "Cannot capture transaction in state" in str(excinfo.value)
        mock_client.capture_order.assert_not_called()


@pytest.mark.asyncio
async def test_capture_persists_CAPTURED_state():
    """Regression: capture must leave txn CAPTURED (not roll back to APPROVED)."""
    from src.adapters.paypal.routes import capture_order, CaptureOrderRequest
    store = get_transaction_store()
    txn = await store.create_transaction(amount="1.00", currency="USD", items=[])
    await store.transition_state(txn.id, TransactionState.VALIDATED)
    await store.transition_state(txn.id, TransactionState.PAYPAL_CREATED)
    await store.transition_state(txn.id, TransactionState.APPROVED)
    await store.add_paypal_order_index("ORD-OK", txn.id)
    with patch("src.adapters.paypal.routes.get_paypal_client") as mock_get:
        mock_client = MagicMock()
        mock_client.capture_order = AsyncMock(return_value={
            "id": "CAP-1", "status": "COMPLETED",
            "purchase_units": [{"payments": {"captures": [{"id": "CAP-1"}]}}],
        })
        mock_get.return_value = mock_client
        req = CaptureOrderRequest(idempotency_key=None)
        await capture_order("ORD-OK", req, {"_id": "u1"})
    fresh = await store.get_transaction(txn.id)
    assert fresh.state == TransactionState.CAPTURED
    assert fresh.paypal_capture_id == "CAP-1"


# ── Coverage for shipped APIs ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_indexes_are_o1():
    store = get_transaction_store()
    txn = await store.create_transaction(amount="1.00", currency="USD", items=[])
    await store.add_paypal_order_index("ORD-X", txn.id)
    await store.add_paypal_capture_index("CAP-X", txn.id)
    assert (await store.get_by_paypal_order_id("ORD-X")).id == txn.id
    assert (await store.get_by_paypal_capture_id("CAP-X")).id == txn.id


@pytest.mark.asyncio
async def test_list_transactions_returns_all():
    store = get_transaction_store()
    a = await store.create_transaction(amount="1.00", currency="USD", items=[])
    b = await store.create_transaction(amount="2.00", currency="USD", items=[])
    assert {t.id for t in await store.list_transactions()} == {a.id, b.id}


@pytest.mark.asyncio
async def test_multi_llm_failover():
    good = PaymentIntent(
        payer_name="Test", payer_email="t@x.com",
        amount="100.00", currency="USD",
        items=[OrderItem(name="I", quantity=1, unit_amount="100.00")],
        due_date=None, type="invoice", confidence=0.9,
        raw_input_hash="a" * 64,
        raw_input="test input", extracted_from="test",
    )
    with patch("src.adapters.paypal.axelr_ai._extract_with_openai",
               side_effect=Exception("down")) as oa, \
         patch("src.adapters.paypal.axelr_ai._extract_with_anthropic",
               new=AsyncMock(return_value=good)) as an, \
         patch("src.adapters.paypal.axelr_ai.get_core_module") as mock_core:
        inst = MagicMock()
        inst.settings.openai_api_key = "k1"
        inst.settings.anthropic_api_key = "k2"
        mock_core.return_value = inst
        out = await extract_payment_intent(SAMPLE1)
        assert oa.called and an.called
        assert Decimal(out.amount) > 0


@pytest.mark.asyncio
async def test_webhook_signature_false_when_webhook_id_missing():
    get_core_module().settings.webhook_id = None
    ok = await get_paypal_client().verify_webhook_signature({}, "{}")
    assert ok is False


@pytest.mark.asyncio
async def test_audit_log_redacts_secrets():
    await log_audit(
        event_type="test.event",
        user_id="u1",
        timestamp="2024-01-01T00:00:00Z",
        metadata={"k": "v", "client_secret": "SHOULD_BE_REDACTED"},
        success=True
    )
    logs = get_audit_logs(user_id="u1")
    assert any(l.event_type == "test.event" for l in logs)
    assert any(l.metadata.get("client_secret") == "***" for l in logs)


def test_regex_handles_bare_currency_amounts():
    intent = _regex_fallback_extraction("Payout 1000 EUR by 2024-11-15", "0" * 64)
    assert intent.currency == "EUR"
    assert Decimal(intent.amount) == Decimal("1000")