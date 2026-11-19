"""Axelr PayPal — core domain: settings, lifecycle, health, transaction
state machine, pluggable store, and audit log.

Honest note: this file has zero external I/O at import time. It can be
imported and unit-tested with no env vars. `init()` is idempotent; calling
`get_core_module()` before `init()` returns safe defaults.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .schemas import (
    VALID_TRANSITIONS,
    TransactionState,
)

MODULE_VERSION = "1.2.0"

logger = logging.getLogger("axelr.paypal.core")


# ── Settings ────────────────────────────────────────────────────────────────
class ModuleSettings(BaseModel):
    """All fields optional with safe defaults — import must never crash."""
    model_config = ConfigDict(extra="ignore")
    client_id: str = ""
    client_secret: str = ""
    environment: str = Field("sandbox", pattern="^(sandbox|live)$")
    webhook_id: Optional[str] = None
    redis_url: Optional[str] = None
    log_redact_auth: bool = True
    webhook_rate_limit: int = Field(100, ge=1, le=10_000)
    idempotency_ttl: int = Field(86_400, ge=3_600)
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None


class HealthStatus(BaseModel):
    healthy: bool
    version: str
    timestamp: str
    transaction_counts: Dict[str, int]
    http_client_connected: bool
    last_shutdown: Optional[str] = None


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Core module ─────────────────────────────────────────────────────────────
class PayPalCoreModule:
    _instance: Optional["PayPalCoreModule"] = None
    _http_client: Optional[httpx.AsyncClient] = None
    _initialized: bool = False
    _shutdown_time: Optional[datetime] = None
    _transaction_counts: Dict[str, int] = {
        s.value: 0 for s in TransactionState
    }

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        # Safe defaults so attribute access never fails.
        if not hasattr(self, "settings"):
            self.settings = ModuleSettings()

    @classmethod
    def get_instance(cls) -> "PayPalCoreModule":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def init(self, settings: Optional[ModuleSettings] = None) -> None:
        if self._initialized:
            logger.debug("core already initialized")
            return
        if settings is None:
            settings = ModuleSettings(
                client_id=os.getenv("PAYPAL_CLIENT_ID", ""),
                client_secret=os.getenv("PAYPAL_CLIENT_SECRET", ""),
                environment=os.getenv("PAYPAL_ENVIRONMENT", "sandbox"),
                webhook_id=os.getenv("PAYPAL_WEBHOOK_ID") or None,
                redis_url=os.getenv("REDIS_URL") or None,
                openai_api_key=os.getenv("OPENAI_API_KEY") or None,
                anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
            )
        self.settings = settings
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                timeout=30.0,
                limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            )
        self._initialized = True
        logger.info("core v%s initialized env=%s configured=%s",
                    MODULE_VERSION, settings.environment, self.is_configured)

    async def shutdown(self) -> None:
        if not self._initialized:
            return
        await self.close_http()
        self._shutdown_time = datetime.now(timezone.utc)
        self._initialized = False
        logger.info("core shutdown complete")

    async def close_http(self) -> None:
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()

    @property
    def is_configured(self) -> bool:
        return bool(self.settings.client_id and self.settings.client_secret)

    def is_healthy(self) -> bool:
        return (
            self._initialized
            and self._http_client is not None
            and not self._http_client.is_closed
            and self.is_configured
        )

    def health_probe(self) -> HealthStatus:
        return HealthStatus(
            healthy=self.is_healthy(),
            version=MODULE_VERSION,
            timestamp=_utcnow_iso(),
            transaction_counts=self._transaction_counts.copy(),
            http_client_connected=bool(
                self._http_client and not self._http_client.is_closed
            ),
            last_shutdown=(
                self._shutdown_time.isoformat()
                if self._shutdown_time else None
            ),
        )

    def increment_transaction_state(self, state: TransactionState | str) -> None:
        key = state.value if isinstance(state, TransactionState) else state
        if key in self._transaction_counts:
            self._transaction_counts[key] += 1

    def decrement_transaction_state(self, state: TransactionState | str) -> None:
        key = state.value if isinstance(state, TransactionState) else state
        if self._transaction_counts.get(key, 0) > 0:
            self._transaction_counts[key] -= 1


_core_instance: Optional[PayPalCoreModule] = None


def get_core_module() -> PayPalCoreModule:
    global _core_instance
    if _core_instance is None:
        _core_instance = PayPalCoreModule()
    return _core_instance


# Module-level lifecycle helpers (safe to call before init).
def init(settings: Optional[ModuleSettings] = None) -> None:
    get_core_module().init(settings)


async def shutdown() -> None:
    await get_core_module().shutdown()


async def close_http() -> None:
    await get_core_module().close_http()


def is_healthy() -> bool:
    return get_core_module().is_healthy()


def health_probe() -> HealthStatus:
    return get_core_module().health_probe()


def increment_transaction_state(state: TransactionState | str) -> None:
    get_core_module().increment_transaction_state(state)


# ── Transaction model + store ───────────────────────────────────────────────
try:
    import redis.asyncio as _redis
except ImportError:  # redis is optional for the demo path
    _redis = None  # type: ignore[assignment]


class Transaction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    state: TransactionState
    paypal_order_id: Optional[str] = None
    paypal_capture_id: Optional[str] = None
    paypal_refund_id: Optional[str] = None
    amount: str
    currency: str
    items: List[Dict[str, Any]]
    created_at: str
    updated_at: str
    history: List[Dict[str, Any]]
    metadata: Optional[Dict[str, Any]] = None

    def can_transition_to(self, s: TransactionState) -> bool:
        return s in VALID_TRANSITIONS.get(self.state, set())


class TransactionStore:
    _instance: Optional["TransactionStore"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        self.redis: Any = None
        self.in_memory_store: Dict[str, Dict[str, Any]] = {}
        self._order_index: Dict[str, str] = {}
        self._capture_index: Dict[str, str] = {}
        self._processed_webhook_ids: set[str] = set()
        self._storage_initialized = False

    @classmethod
    def get_instance(cls) -> "TransactionStore":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _initialize_storage(self) -> None:
        if self._storage_initialized:
            # Re-check: init() may have set redis_url after first use
            core = get_core_module()
            if core.settings.redis_url and self.redis is None and _redis is not None:
                try:
                    self.redis = _redis.from_url(core.settings.redis_url)
                    logger.info("redis transaction store attached late")
                except Exception as e:
                    logger.error("late redis init failed: %s", e)
            return
        core = get_core_module()
        if core.settings.redis_url and _redis is not None:
            try:
                self.redis = _redis.from_url(core.settings.redis_url)
                logger.info("redis transaction store active")
            except Exception as e:
                logger.error("redis init failed, in-memory fallback: %s", e)
                self.redis = None
        else:
            logger.warning("no redis — in-memory store (NOT for production)")
        self._storage_initialized = True

    # ── CRUD ─────────────────────────────────────────────────────────────
    async def create_transaction(
        self, *, amount: str, currency: str,
        items: List[Dict[str, Any]], metadata: Optional[Dict[str, Any]] = None,
    ) -> Transaction:
        self._initialize_storage()
        tid = str(uuid.uuid4())
        now = _utcnow_iso()
        txn = Transaction(
            id=tid, state=TransactionState.INITIATED,
            amount=amount, currency=currency, items=items,
            created_at=now, updated_at=now,
            history=[{"state": TransactionState.INITIATED.value,
                      "timestamp": now, "reason": "created"}],
            metadata=metadata or {},
        )
        await self._save(txn)
        get_core_module().increment_transaction_state(TransactionState.INITIATED)
        logger.info("txn created id=%s state=INITIATED", tid)
        return txn

    async def get_transaction(self, txn_id: str) -> Optional[Transaction]:
        self._initialize_storage()
        if self.redis is not None:
            raw = await self.redis.get(f"txn:{txn_id}")
            if raw is None:
                return None
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode()
            return Transaction(**json.loads(raw))
        data = self.in_memory_store.get(f"txn:{txn_id}")
        return Transaction(**data) if data else None

    async def list_transactions(self) -> List[Transaction]:
        self._initialize_storage()
        if self.redis is not None:
            out: List[Transaction] = []
            async for key in self.redis.scan_iter(match="txn:*"):
                raw = await self.redis.get(key)
                if raw is None:
                    continue
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode()
                out.append(Transaction(**json.loads(raw)))
            return out
        return [Transaction(**d) for d in self.in_memory_store.values()]

    async def _save(self, txn: Transaction) -> None:
        payload = txn.model_dump_json()
        if self.redis is not None:
            await self.redis.setex(f"txn:{txn.id}", 86_400 * 30, payload)
        else:
            self.in_memory_store[f"txn:{txn.id}"] = json.loads(payload)

    # ── Transitions ──────────────────────────────────────────────────────
    async def transition_state(
        self, txn_id: str, new_state: TransactionState,
        reason: str = "state update",
    ) -> Transaction:
        txn = await self.get_transaction(txn_id)
        if txn is None:
            raise ValueError(f"transaction {txn_id} not found")
        if not txn.can_transition_to(new_state):
            raise ValueError(
                f"invalid state transition from {txn.state.value} to "
                f"{new_state.value} for transaction {txn_id}"
            )
        old = txn.state
        now = _utcnow_iso()
        txn.state = new_state
        txn.updated_at = now
        txn.history.append({
            "state": new_state.value, "timestamp": now,
            "reason": reason, "previous_state": old.value,
        })
        await self._save(txn)
        core = get_core_module()
        core.decrement_transaction_state(old)
        core.increment_transaction_state(new_state)
        logger.info("txn %s %s→%s (%s)", txn_id, old.value, new_state.value, reason)
        return txn

    # ── Indexes ──────────────────────────────────────────────────────────
    async def add_paypal_order_index(self, oid: str, tid: str) -> None:
        if self.redis is not None:
            await self.redis.setex(f"order_index:{oid}", 86_400 * 30, tid)
        else:
            self._order_index[oid] = tid

    async def add_paypal_capture_index(self, cid: str, tid: str) -> None:
        if self.redis is not None:
            await self.redis.setex(f"capture_index:{cid}", 86_400 * 30, tid)
        else:
            self._capture_index[cid] = tid

    async def get_by_paypal_order_id(self, oid: str) -> Optional[Transaction]:
        self._initialize_storage()
        if self.redis is not None:
            tid = await self.redis.get(f"order_index:{oid}")
            if tid is None:
                return None
            if isinstance(tid, (bytes, bytearray)):
                tid = tid.decode()
            return await self.get_transaction(tid)
        tid = self._order_index.get(oid)
        return await self.get_transaction(tid) if tid else None

    async def get_by_paypal_capture_id(self, cid: str) -> Optional[Transaction]:
        self._initialize_storage()
        if self.redis is not None:
            tid = await self.redis.get(f"capture_index:{cid}")
            if tid is None:
                return None
            if isinstance(tid, (bytes, bytearray)):
                tid = tid.decode()
            return await self.get_transaction(tid)
        tid = self._capture_index.get(cid)
        return await self.get_transaction(tid) if tid else None


_txn_store_instance: Optional[TransactionStore] = None


def get_transaction_store() -> TransactionStore:
    global _txn_store_instance
    if _txn_store_instance is None:
        _txn_store_instance = TransactionStore.get_instance()
    return _txn_store_instance


# ── Auditing ────────────────────────────────────────────────────────────────
@dataclass(slots=True)
class AuditEntry:
    id: str
    event_type: str
    user_id: str
    timestamp: str
    session_id: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    success: bool = True
    error: Optional[str] = None


class AuditEvent(BaseModel):
    """Caller-facing audit event model (used by routes)."""
    model_config = ConfigDict(extra="forbid")
    event_type: str
    user_id: str
    timestamp: str
    session_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


_audit_log: List[AuditEntry] = []
_audit_lock = asyncio.Lock()


def _redact(meta: Dict[str, Any]) -> Dict[str, Any]:
    redacted: Dict[str, Any] = {}
    for k, v in meta.items():
        kl = k.lower()
        if any(s in kl for s in ("secret", "token", "authorization", "password")):
            redacted[k] = "***"
        else:
            redacted[k] = v
    return redacted


async def log_audit(event: AuditEvent) -> None:
    entry = AuditEntry(
        id=f"audit_{uuid.uuid4().hex[:16]}",
        event_type=event.event_type,
        user_id=event.user_id,
        timestamp=event.timestamp,
        session_id=event.session_id or f"session_{uuid.uuid4().hex[:8]}",
        metadata=_redact(event.metadata),
    )
    async with _audit_lock:
        _audit_log.append(entry)
    logger.info("audit %s user=%s meta=%s",
                entry.event_type, entry.user_id, entry.metadata)


async def log_audit_event(event: Dict[str, Any]) -> None:
    """Backward-compat dict form used by webhooks.py."""
    await log_audit(AuditEvent(
        event_type=event.get("event_type", "unknown"),
        user_id=event.get("user_id", "anon"),
        timestamp=event.get("timestamp", _utcnow_iso()),
        session_id=event.get("session_id"),
        metadata=event.get("metadata", {}),
    ))


def get_audit_logs(
    user_id: Optional[str] = None,
    event_type: Optional[str] = None,
    limit: int = 100,
) -> List[AuditEntry]:
    out = list(_audit_log)
    if user_id:
        out = [e for e in out if e.user_id == user_id]
    if event_type:
        out = [e for e in out if e.event_type == event_type]
    out.sort(key=lambda e: e.timestamp, reverse=True)
    return out[:limit]


def get_audit_stats() -> Dict[str, Any]:
    total = len(_audit_log)
    ops: Dict[str, int] = {}
    for e in _audit_log:
        ops[e.event_type] = ops.get(e.event_type, 0) + 1
    success = sum(1 for e in _audit_log if e.success)
    return {
        "total_entries": total,
        "operations": ops,
        "success_rate": round(success / total, 4) if total else 0.0,
        "last_updated": _utcnow_iso(),
    }


__all__ = [
    "MODULE_VERSION",
    "ModuleSettings", "HealthStatus",
    "PayPalCoreModule", "get_core_module",
    "init", "shutdown", "close_http", "is_healthy", "health_probe",
    "increment_transaction_state",
    "Transaction", "TransactionStore", "get_transaction_store",
    "AuditEvent", "AuditEntry",
    "log_audit", "log_audit_event", "get_audit_logs", "get_audit_stats",
]
