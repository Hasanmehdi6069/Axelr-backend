import json
import pytest
from unittest.mock import patch, AsyncMock
from src.adapters.paypal.transactions import get_transaction_store, TransactionState
from src.adapters.paypal.webhooks import process_webhook_event

@pytest.mark.asyncio
async def test_debug_webhook():
    store = get_transaction_store()
    print(f"Store instance ID: {id(store)}")
    
    # Clear any existing data
    store.in_memory_store.clear()
    store._order_index.clear()
    store._processed_webhook_ids.clear()
    
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
    
    print(f"Store in-memory: {store.in_memory_store}")
    print(f"Order index: {store._order_index}")
    
    # Now retrieve by order ID
    txn_by_order = await store.get_by_paypal_order_id("TEST-ORDER-123")
    print(f"Retrieved by order ID: {txn_by_order.id if txn_by_order else 'None'}, state: {txn_by_order.state if txn_by_order else 'None'}")
    
    # Now check what the webhook handler gets
    from src.adapters.paypal.webhooks import PayPalWebhookHandler
    handler = PayPalWebhookHandler.get_instance()
    print(f"Handler store instance ID: {id(handler.store)}")
    
    txn_by_handler = await handler.store.get_by_paypal_order_id("TEST-ORDER-123")
    print(f"Handler retrieved by order ID: {txn_by_handler.id if txn_by_handler else 'None'}, state: {txn_by_handler.state if txn_by_handler else 'None'}")
    
    # Now try the actual webhook
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
        try:
            r1 = await process_webhook_event({"PAYPAL-TRANSMISSION-ID": "test-id-1"}, body)
            print(f"Webhook result: {r1}")
        except Exception as e:
            print(f"Webhook error: {e}")
            # Check store state after error
            print(f"Store after error: {store.in_memory_store}")

if __name__ == "__main__":
    import asyncio
    asyncio.run(test_debug_webhook())