import asyncio
import json
from src.adapters.paypal.transactions import get_transaction_store, TransactionState

async def debug_test():
    store = get_transaction_store()
    print(f"Store instance: {id(store)}")
    
    # Clear any existing data
    store.in_memory_store.clear()
    store._order_index.clear()
    store._processed_webhook_ids.clear()
    
    txn = await store.create_transaction(amount="100.00", currency="USD", items=[])
    print(f"Created transaction: {txn.id}, state: {txn.state}")
    print(f"Store after create: {store.in_memory_store}")
    
    await store.transition_state(txn.id, TransactionState.VALIDATED)
    txn = await store.get_transaction(txn.id)
    print(f"After VALIDATED: {txn.state}")
    
    await store.transition_state(txn.id, TransactionState.PAYPAL_CREATED)
    txn = await store.get_transaction(txn.id)
    print(f"After PAYPAL_CREATED: {txn.state}")
    
    txn.paypal_order_id = "TEST-ORDER-123"
    await store._save(txn)
    await store.add_paypal_order_index("TEST-ORDER-123", txn.id)
    
    print(f"Store after save: {store.in_memory_store}")
    print(f"Order index: {store._order_index}")
    
    # Now retrieve the transaction by order ID
    txn_by_order = await store.get_by_paypal_order_id("TEST-ORDER-123")
    print(f"Retrieved by order ID: {txn_by_order.id if txn_by_order else 'None'}, state: {txn_by_order.state if txn_by_order else 'None'}")
    
    # Now create a new store instance and check
    store2 = get_transaction_store()
    print(f"Store2 instance: {id(store2)}")
    txn_by_order2 = await store2.get_by_paypal_order_id("TEST-ORDER-123")
    print(f"Retrieved by order ID from store2: {txn_by_order2.id if txn_by_order2 else 'None'}, state: {txn_by_order2.state if txn_by_order2 else 'None'}")

if __name__ == "__main__":
    asyncio.run(debug_test())