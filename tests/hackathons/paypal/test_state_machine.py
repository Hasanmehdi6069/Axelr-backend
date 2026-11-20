import json
import pytest
from unittest.mock import patch, AsyncMock
from src.adapters.paypal.transactions import get_transaction_store, TransactionState, Transaction
from src.adapters.paypal.webhooks import process_webhook_event
from src.adapters.paypal.routes import capture_order
from src.adapters.paypal.schemas import CaptureOrderRequest
from src.adapters.paypal.core import get_core_module


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


_LOCAL_IDEM = {}


async def test_state_machine_transitions():
    store = get_transaction_store()
    txn = await store.create_transaction(amount="100.00", currency="USD", items=[])
    assert txn.state == TransactionState.INITIATED

    with pytest.raises(ValueError):
        await store.transition_state(txn.id, TransactionState.APPROVED)

    await store.transition_state(txn.id, TransactionState.VALIDATED)
    txn = await store.get_transaction(txn.id)
    assert txn.state == TransactionState.VALIDATED

    await store.transition_state(txn.id, TransactionState.PAYPAL_CREATED)
    txn = await store.get_transaction(txn.id)
    assert txn.state == TransactionState.PAYPAL_CREATED

    await store.transition_state(txn.id, TransactionState.APPROVED)
    txn = await store.get_transaction(txn.id)
    assert txn.state == TransactionState.APPROVED

    await store.transition_state(txn.id, TransactionState.CAPTURED)
    txn = await store.get_transaction(txn.id)
    assert txn.state == TransactionState.CAPTURED


@pytest.mark.asyncio
async def test_webhook_duplicate_event_processed_once():
    store = get_transaction_store()
    txn = await store.create_transaction(amount="100.00", currency="USD", items=[])
    await store.transition_state(txn.id, TransactionState.VALIDATED)
    await store.transition_state(txn.id, TransactionState.PAYPAL_CREATED)
    txn = await store.get_transaction(txn.id)
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
        r1 = await process_webhook_event({"PAYPAL-TRANSMISSION-ID": "evt-123456"}, body)
        assert r1["success"] is True
        after1 = await store.get_transaction(txn.id)
        assert after1.state == TransactionState.APPROVED

        r2 = await process_webhook_event({"PAYPAL-TRANSMISSION-ID": "evt-123456"}, body)
        assert r2.get("skipped") is True
        after2 = await store.get_transaction(txn.id)
        assert after2.state == TransactionState.APPROVED


@pytest.mark.asyncio
async def test_capture_rejected_before_money_moves():
    store = get_transaction_store()
    txn = await store.create_transaction(amount="50.00", currency="USD", items=[])
    req = CaptureOrderRequest(order_id="TESTORDERID", idempotency_key=None)
    with pytest.raises(Exception):
        await capture_order(txn.id, req)


@pytest.mark.asyncio
async def test_capture_persists_CAPTURED_state():
    store = get_transaction_store()
    txn = await store.create_transaction(amount="75.00", currency="USD", items=[])
    await store.transition_state(txn.id, TransactionState.VALIDATED)
    await store.transition_state(txn.id, TransactionState.PAYPAL_CREATED)
    await store.transition_state(txn.id, TransactionState.APPROVED)

    with patch(
        "src.adapters.paypal.client.PayPalClient.capture_order",
        new=AsyncMock(return_value={"status": "COMPLETED", "id": "cap-123"}),
    ):
        req = CaptureOrderRequest(order_id="TESTORDERID", idempotency_key=None)
        resp = await capture_order(txn.id, req)
        assert resp is not None

    after = await store.get_transaction(txn.id)
    assert after.state == TransactionState.CAPTURED
    assert after.paypal_capture_id == "cap-123"


@pytest.mark.asyncio
async def test_multi_llm_failover():
    from src.adapters.paypal.axelr_ai import extract_payment_intent

    with patch(
        "src.adapters.paypal.axelr_ai._extract_with_openai",
        side_effect=Exception("OpenAI down"),
    ), patch(
        "src.adapters.paypal.axelr_ai._extract_with_anthropic",
        side_effect=Exception("Anthropic down"),
    ):
        result = await extract_payment_intent("Order: 2x Apples @ $5 = $10")
        assert result is not None
        assert result.amount is not None