"""Transaction state machine and storage with pluggable backends (in-memory + Redis)"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

import redis.asyncio as redis
from pydantic import BaseModel, ConfigDict

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
    TransactionState.REFUNDED: set(),  # Terminal state
}


class Transaction(BaseModel):
    """Model representing a single payment transaction with full audit trail"""

    model_config = ConfigDict(extra="forbid")

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
        self.core = get_core_module()
        self.redis: Optional[redis.Redis] = None
        self.in_memory_store: Dict[str, Dict[str, Any]] = {}
        # In-memory indexes for demo mode (O(1) lookups without Redis)
        self._order_index: Dict[str, str] = {}  # paypal_order_id -> txn_id
        self._capture_index: Dict[str, str] = {}  # paypal_capture_id -> txn_id
        self._storage_initialized = False

    def _initialize_storage(self) -> None:
        """Initialize storage backend (Redis or in-memory) only after core is initialized"""
        if self._storage_initialized:
            return

        if not self.core._initialized:
            raise RuntimeError("Core module must be initialized before accessing transaction store")

        # Initialize Redis if URL is provided
        if self.core.settings.redis_url:
            try:
                self.redis = redis.from_url(self.core.settings.redis_url)
                logger.info("Redis transaction store initialized")
            except Exception as e:
                logger.error(f"Failed to initialize Redis, falling back to in-memory: {str(e)}")
                self.redis = None
        else:
            logger.warning("Redis URL not configured, using in-memory transaction store (not for production)")

        self._storage_initialized = True

    @classmethod
    def get_instance(cls) -> "TransactionStore":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def create_transaction(
        self, amount: str, currency: str, items: List[Dict[str, Any]], metadata: Optional[Dict] = None
    ) -> Transaction:
        """Create a new transaction in INITIATED state"""
        self._initialize_storage()
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
            metadata=metadata or {},
        )

        await self._save(txn)
        self.core.increment_transaction_state(TransactionState.INITIATED)
        logger.info(f"Created new transaction {txn_id} in state INITIATED")
        return txn

    async def transition_state(
        self, txn_id: str, new_state: TransactionState, reason: str = "State updated"
    ) -> Transaction:
        """Transition a transaction to a new state with validation and audit logging"""
        self._initialize_storage()
        txn = await self.get_transaction(txn_id)
        if not txn:
            raise ValueError(f"Transaction {txn_id} not found")

        if not txn.can_transition_to(new_state):
            error = f"Invalid state transition from {txn.state} to {new_state} for transaction {txn_id}"
            logger.error(error)
            raise ValueError(error)

        old_state = txn.state
        now = datetime.utcnow().isoformat() + "Z"
        txn.state = new_state
        txn.updated_at = now
        txn.history.append({"state": new_state, "timestamp": now, "reason": reason, "previous_state": old_state})

        await self._save(txn)
        # Update core counters (thread-safe)
        if old_state in self.core._transaction_counts and self.core._transaction_counts[old_state] > 0:
            self.core._transaction_counts[old_state] -= 1
        if new_state in self.core._transaction_counts:
            self.core._transaction_counts[new_state] += 1
        logger.info(f"Transaction {txn_id} transitioned from {old_state} to {new_state}: {reason}")
        return txn

    async def get_transaction(self, txn_id: str) -> Optional[Transaction]:
        """Retrieve a transaction by ID"""
        self._initialize_storage()
        if self.redis:
            data = await self.redis.get(f"txn:{txn_id}")
            if not data:
                return None
            return Transaction(**json.loads(data))
        else:
            data = self.in_memory_store.get(f"txn:{txn_id}")
            return Transaction(**data) if data else None

    async def _save(self, txn: Transaction) -> None:
        """Internal save to active backend"""
        txn_data = txn.model_dump_json()
        if self.redis:
            await self.redis.setex(f"txn:{txn.id}", 86400 * 30, txn_data)  # Store for 30 days
        else:
            self.in_memory_store[f"txn:{txn.id}"] = json.loads(txn_data)

    async def add_paypal_order_index(self, paypal_order_id: str, txn_id: str) -> None:
        """Add index to find transaction by PayPal order ID (O(1) lookup)"""
        if self.redis:
            await self.redis.setex(f"order_index:{paypal_order_id}", 86400 * 30, txn_id)
        else:
            self._order_index[paypal_order_id] = txn_id

    async def add_paypal_capture_index(self, paypal_capture_id: str, txn_id: str) -> None:
        """Add index to find transaction by PayPal capture ID (O(1) lookup)"""
        if self.redis:
            await self.redis.setex(f"capture_index:{paypal_capture_id}", 86400 * 30, txn_id)
        else:
            self._capture_index[paypal_capture_id] = txn_id

    async def get_by_paypal_order_id(self, paypal_order_id: str) -> Optional[Transaction]:
        """Find transaction by PayPal order ID - production-ready O(1) lookup"""
        self._initialize_storage()
        if self.redis:
            txn_id = await self.redis.get(f"order_index:{paypal_order_id}")
            if not txn_id:
                return None
            return await self.get_transaction(txn_id)
        else:
            txn_id = self._order_index.get(paypal_order_id)
            return await self.get_transaction(txn_id) if txn_id else None

    async def get_by_paypal_capture_id(self, paypal_capture_id: str) -> Optional[Transaction]:
        """Find transaction by PayPal capture ID - production-ready O(1) lookup"""
        self._initialize_storage()
        if self.redis:
            txn_id = await self.redis.get(f"capture_index:{paypal_capture_id}")
            if not txn_id:
                return None
            return await self.get_transaction(txn_id)
        else:
            txn_id = self._capture_index.get(paypal_capture_id)
            return await self.get_transaction(txn_id) if txn_id else None


def get_transaction_store() -> TransactionStore:
    return TransactionStore.get_instance()
