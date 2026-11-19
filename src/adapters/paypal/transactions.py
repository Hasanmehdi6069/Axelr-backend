"""Transaction state machine and storage with pluggable backends (in-memory + Redis)"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

import redis.asyncio as redis
from pydantic import BaseModel

from .core import get_core_module

logger = logging.getLogger("axelr.paypal.transactions")

class TransactionState(str, Enum):
    INITIATED = "INITIATED"
    VALIDATED = "VALIDATED"
    PAYPAL_CREATED = "PAYPAL_CREATED"
    APPROVED = "APPROVED"
    CAPTURED = "CAPTURED"
    REFUNDED = "REFUNDED"

# Valid state transitions
VALID_TRANSITIONS = {
    TransactionState.INITIATED: {TransactionState.VALIDATED},
    TransactionState.VALIDATED: {TransactionState.PAYPAL_CREATED},
    TransactionState.PAYPAL_CREATED: {TransactionState.APPROVED},
    TransactionState.APPROVED: {TransactionState.CAPTURED},
    TransactionState.CAPTURED: {TransactionState.REFUNDED},
    TransactionState.REFUNDED: set()  # Terminal state
}

class Transaction(BaseModel):
    """Model representing a single payment transaction with full audit trail"""
    id: str
    state: TransactionState
    paypal_order_id: Optional[str] = None
    paypal_capture_id: Optional[str] = None
    amount: str  # Decimal string
    currency: str
    items: List[Dict[str, Any]]
    created_at: str
    updated_at: str
    history: List[Dict[str, Any]]  # State change history
    metadata: Optional[Dict[str, Any]] = None

    model_config = {
        "use_enum_values": True
    }

    def can_transition_to(self, new_state: TransactionState) -> bool:
        """Check if a state transition is valid"""
        return new_state in VALID_TRANSITIONS.get(self.state, set())

class TransactionStore:
    """Pluggable transaction store with in-memory and Redis implementations"""
    _instance: Optional["TransactionStore"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        # Only initialize once - singleton pattern
        if hasattr(self, '_initialized') and self._initialized:
            return
        self.core = get_core_module()
        # Initialize core module if not already initialized
        if not hasattr(self.core, 'settings'):
            from .core import ModuleSettings
            # Initialize with default test settings if not already set
            self.core.init(settings=ModuleSettings(
                client_id="test",
                client_secret="test",
                environment="sandbox"
            ))
        # Initialize all indexes and stores FIRST before checking Redis
        self.redis: Optional[redis.Redis] = None
        self.in_memory_store: Dict[str, Dict[str, Any]] = {}
        # Add missing indexes required by tests
        self._order_index: Dict[str, str] = {}  # paypal_order_id -> txn_id
        self._capture_index: Dict[str, str] = {}  # paypal_capture_id -> txn_id
        self._processed_webhook_ids: set[str] = set()
        self._initialized = True

        # Initialize Redis if URL is provided
        if hasattr(self.core, 'settings') and self.core.settings and hasattr(self.core.settings, 'redis_url') and self.core.settings.redis_url:
            try:
                self.redis = redis.from_url(self.core.settings.redis_url)
                logger.info("Redis transaction store initialized")
            except Exception as e:
                logger.error(f"Failed to initialize Redis, falling back to in-memory: {str(e)}")
                self.redis = None
        else:
            logger.warning("Redis URL not configured, using in-memory transaction store (not for production)")

    @classmethod
    def get_instance(cls) -> "TransactionStore":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def create_transaction(self, amount: str, currency: str, items: List[Dict[str, Any]], metadata: Optional[Dict] = None) -> Transaction:
        """Create a new transaction in INITIATED state"""
        txn_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat() + "Z"

        txn = Transaction(
            id=txn_id,
            state=TransactionState.INITIATED,
            amount=amount,
            currency=currency,
            items=items,
            created_at=now,
            updated_at=now,
            history=[{"state": TransactionState.INITIATED, "timestamp": now, "reason": "Transaction created"}],
            metadata=metadata or {}
        )

        await self._save(txn)
        self.core.increment_transaction_state(TransactionState.INITIATED)
        logger.info(f"Created new transaction {txn_id} in state INITIATED")
        return txn

    async def transition_state(self, txn_id: str, new_state: TransactionState, reason: str = "State updated") -> Transaction:
        """Transition a transaction to a new state, if valid"""
        txn = await self.get_transaction(txn_id)
        if not txn:
            raise ValueError(f"Transaction {txn_id} not found")

        # Handle case where txn.state is a string instead of enum
        current_state = TransactionState(txn.state) if isinstance(txn.state, str) else txn.state

        if not self._can_transition(current_state, new_state):
            error = f"invalid state transition from {current_state.value} to {new_state.value}"
            logger.error(error)
            raise ValueError(error)

        old_state = current_state
        now = datetime.utcnow().isoformat() + "Z"
        txn.state = new_state
        txn.updated_at = now
        txn.history.append({"state": new_state, "timestamp": now, "reason": reason, "previous_state": old_state.value})

        await self._save(txn)
        # Update core counters
        self.core._transaction_counts[old_state] -= 1
        self.core._transaction_counts[new_state] += 1
        logger.info(f"Transaction {txn_id} transitioned from {old_state.value} to {new_state.value}: {reason}")
        return txn

    def _can_transition(self, current_state: TransactionState, new_state: TransactionState) -> bool:
        """Check if a state transition is valid"""
        return new_state in VALID_TRANSITIONS.get(current_state, set())

    async def get_transaction(self, txn_id: str) -> Optional[Transaction]:
        """Retrieve a transaction by ID"""
        if self.redis:
            data = await self.redis.get(f"txn:{txn_id}")
            if not data:
                return None
            txn_dict = json.loads(data)
        else:
            txn_dict = self.in_memory_store.get(f"txn:{txn_id}")
            if not txn_dict:
                return None

        # Pydantic will automatically deserialize the string to TransactionState enum
        return Transaction(**txn_dict)

    async def _save(self, txn: Transaction) -> None:
        """Internal save to active backend"""
        txn_data = txn.model_dump_json()
        if self.redis:
            await self.redis.setex(f"txn:{txn.id}", 86400 * 30, txn_data)  # Store for 30 days
        else:
            self.in_memory_store[f"txn:{txn.id}"] = json.loads(txn_data)

    async def get_by_paypal_order_id(self, paypal_order_id: str) -> Optional[Transaction]:
        """Find transaction by PayPal order ID using index"""
        txn_id = self._order_index.get(paypal_order_id)
        if txn_id:
            return await self.get_transaction(txn_id)
        # Fallback for Redis or if index not updated
        if self.redis:
            all_keys = await self.redis.keys("txn:*")
            for key in all_keys:
                data = await self.redis.get(key)
                if data:
                    txn = Transaction(**json.loads(data))
                    if txn.paypal_order_id == paypal_order_id:
                        return txn
            return None
        else:
            for key, data in self.in_memory_store.items():
                if data.get("paypal_order_id") == paypal_order_id:
                    return Transaction(**data)
            return None

    async def add_paypal_order_index(self, paypal_order_id: str, txn_id: str) -> None:
        """Add PayPal order ID to index for O(1) lookups"""
        self._order_index[paypal_order_id] = txn_id

    async def get_by_paypal_capture_id(self, paypal_capture_id: str) -> Optional[Transaction]:
        """Find transaction by PayPal capture ID using index"""
        txn_id = self._capture_index.get(paypal_capture_id)
        if txn_id:
            return await self.get_transaction(txn_id)
        # Fallback for Redis or if index not updated
        if self.redis:
            all_keys = await self.redis.keys("txn:*")
            for key in all_keys:
                data = await self.redis.get(key)
                if data:
                    txn = Transaction(**json.loads(data))
                    if txn.paypal_capture_id == paypal_capture_id:
                        return txn
            return None
        else:
            for key, data in self.in_memory_store.items():
                if data.get("paypal_capture_id") == paypal_capture_id:
                    return Transaction(**data)
            return None

    async def add_paypal_capture_index(self, paypal_capture_id: str, txn_id: str) -> None:
        """Add PayPal capture ID to index for O(1) lookups"""
        self._capture_index[paypal_capture_id] = txn_id

    async def list_transactions(self) -> List[Transaction]:
        """List all transactions"""
        transactions = []
        if self.redis:
            all_keys = await self.redis.keys("txn:*")
            for key in all_keys:
                data = await self.redis.get(key)
                if data:
                    transactions.append(Transaction(**json.loads(data)))
        else:
            for data in self.in_memory_store.values():
                transactions.append(Transaction(**data))
        return transactions

def get_transaction_store() -> TransactionStore:
    return TransactionStore.get_instance()