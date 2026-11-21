import json
from unittest.mock import AsyncMock, patch

import pytest

from src.adapters.paypal.transactions import TransactionState, get_transaction_store
from src.adapters.paypal.webhooks import process_webhook_event


@pytest.mark.asyncio
async def test_webhook_duplicate_event_processed_once():
    store = get_transaction_store()
    print("\n=== TEST START ===")
    print(f"Store instance ID: {id(store)}")
    print(f"Store in-memory before create: {store.in_memory_store}")

    txn = await store.create_transaction(amount="100.00", currency="USD", items=[])
    print(f"Created txn id: {txn.id}, state: {txn.state}")

    await store.transition_state(txn.id, TransactionState.VALIDATED)
    txn = await store.get_transaction(txn.id)
    print(f"After VALIDATED: {txn.state}")

    await store.transition_state(txn.id, TransactionState.PAYPAL_CREATED)
    txn = await store.get_transaction(txn.id)
    print(f"After PAYPAL_CREATED: {txn.state}")

    txn.paypal_order_id = "TEST-ORDER-123"
    await store._save(txn)
    await store.add_paypal_order_index("TEST-ORDER-123", txn.id)

    print(f"Store in-memory after save: {store.in_memory_store}")
    print(f"Order index: {store._order_index}")

    # Now retrieve by order ID
    txn_by_order = await store.get_by_paypal_order_id("TEST-ORDER-123")
    print(
        f"Retrieved by order ID before webhook: {txn_by_order.id if txn_by_order else 'None'}, state: {txn_by_order.state if txn_by_order else 'None'}"
    )

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
        print("\n=== CALLING WEBHOOK ===")
        r1 = await process_webhook_event({"PAYPAL-TRANSMISSION-ID": "test-id-1"}, body)
        print(f"Webhook result: {r1}")
        assert r1["success"] is True
        after1 = await store.get_transaction(txn.id)
        print(f"After webhook 1: {after1.state}")
        assert after1.state == TransactionState.APPROVED

        print("\n=== CALLING WEBHOOK AGAIN ===")
        r2 = await process_webhook_event({"PAYPAL-TRANSMISSION-ID": "test-id-1"}, body)
        print(f"Webhook result 2: {r2}")
        assert r2.get("skipped") is True
        after2 = await store.get_transaction(txn.id)
        print(f"After webhook 2: {after2.state}")
        assert after2.state == TransactionState.APPROVED
