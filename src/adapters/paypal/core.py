"""Core PayPal module configuration, lifecycle management, and health checks"""
from __future__ import annotations

import os
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime
import httpx
from pydantic import BaseModel, Field

MODULE_VERSION = "1.0.0"

logger = logging.getLogger("axelr.paypal.core")

class AuditEvent(BaseModel):
    """Audit event model for tracking all PayPal-related actions"""
    timestamp: str
    event_type: str
    transaction_id: Optional[str] = None
    user_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    details: Dict[str, Any] = Field(default_factory=dict)
    success: bool = True

class ModuleSettings(BaseModel):
    """Pydantic model for PayPal module configuration with strict validation"""
    client_id: str = Field(..., min_length=1)
    client_secret: str = Field(..., min_length=1)
    environment: str = Field(default="sandbox", pattern="^(sandbox|live)$")
    webhook_id: Optional[str] = None
    redis_url: Optional[str] = None
    log_redact_auth: bool = Field(default=True)
    webhook_rate_limit: int = Field(default=100, ge=1, le=10000)
    idempotency_ttl: int = Field(default=86400, ge=3600)
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None

class HealthStatus(BaseModel):
    """Structured health check response"""
    healthy: bool
    version: str
    timestamp: str
    transaction_counts: Dict[str, int]
    http_client_connected: bool
    last_shutdown: Optional[str] = None

class PayPalCoreModule:
    """Singleton core module for lifecycle management"""
    _instance: Optional["PayPalCoreModule"] = None
    _http_client: Optional[httpx.AsyncClient] = None
    _initialized: bool = False
    _shutdown_time: Optional[datetime] = None
    _transaction_counts: Dict[str, int] = {
        "INITIATED": 0, "VALIDATED": 0, "PAYPAL_CREATED": 0,
        "APPROVED": 0, "CAPTURED": 0, "REFUNDED": 0
    }
    _audit_logs: List[AuditEvent] = []
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def get_instance(cls) -> "PayPalCoreModule":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def init(self, settings: Optional[ModuleSettings] = None) -> None:
        """Initialize the core module with settings from environment or explicit config"""
        if self._initialized:
            logger.debug("PayPal core module already initialized")
            return

        if not settings:
            client_id = os.getenv("PAYPAL_CLIENT_ID", "")
            client_secret = os.getenv("PAYPAL_CLIENT_SECRET", "")
            environment = os.getenv("PAYPAL_ENVIRONMENT", "sandbox")
            webhook_id = os.getenv("PAYPAL_WEBHOOK_ID")
            redis_url = os.getenv("REDIS_URL")
            
            settings = ModuleSettings(
                client_id=client_id,
                client_secret=client_secret,
                environment=environment,
                webhook_id=webhook_id,
                redis_url=redis_url
            )
        
        self.settings = settings
        self._http_client = httpx.AsyncClient(
            timeout=30.0,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20)
        )
        self._initialized = True
        logger.info(f"✅ PayPal core module v{MODULE_VERSION} initialized in {settings.environment} mode")

    async def shutdown(self) -> None:
        """Gracefully shutdown the module and cleanup resources"""
        if not self._initialized:
            return
            
        await self.close_http()
        self._shutdown_time = datetime.utcnow()
        self._initialized = False
        logger.info("🛑 PayPal core module shutdown complete")

    async def close_http(self) -> None:
        """Close the HTTP client connection pool"""
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()
            logger.debug("HTTP client connections closed")

    def is_healthy(self) -> bool:
        """Check if the module is in a healthy state"""
        if not self._initialized:
            return False
        if not self._http_client or self._http_client.is_closed:
            return False
        if not self.settings.client_id or not self.settings.client_secret:
            return False
        return True

    def health_probe(self) -> HealthStatus:
        """Return detailed health status for monitoring"""
        return HealthStatus(
            healthy=self.is_healthy(),
            version=MODULE_VERSION,
            timestamp=datetime.utcnow().isoformat() + "Z",
            transaction_counts=self._transaction_counts.copy(),
            http_client_connected=bool(self._http_client and not self._http_client.is_closed),
            last_shutdown=self._shutdown_time.isoformat() + "Z" if self._shutdown_time else None
        )

    def increment_transaction_state(self, state: str) -> None:
        """Thread-safe increment of transaction state counter"""
        if state in self._transaction_counts:
            self._transaction_counts[state] += 1

    def add_audit_event(self, event: AuditEvent) -> None:
        """Add an audit event to the log"""
        self._audit_logs.append(event)

    def get_audit_logs(self) -> List[AuditEvent]:
        """Get all audit logs"""
        return self._audit_logs.copy()

_core_instance = None

def get_core_module() -> PayPalCoreModule:
    global _core_instance
    if _core_instance is None:
        _core_instance = PayPalCoreModule()
    return _core_instance

async def log_audit(event_type: str, transaction_id: Optional[str] = None, user_id: Optional[str] = None, details: Dict[str, Any] = None, metadata: Dict[str, Any] = None, success: bool = True, timestamp: Optional[str] = None) -> None:
    """Helper function to log audit events"""
    module = get_core_module()
    # Redact sensitive fields from metadata and details
    redacted_metadata = metadata.copy() if metadata else {}
    redacted_details = details.copy() if details else {}
    sensitive_fields = ["client_secret", "api_key", "password", "token"]
    for field in sensitive_fields:
        if field in redacted_metadata:
            redacted_metadata[field] = "***"
        if field in redacted_details:
            redacted_details[field] = "***"
    event = AuditEvent(
        timestamp=timestamp or datetime.utcnow().isoformat() + "Z",
        event_type=event_type,
        transaction_id=transaction_id,
        user_id=user_id,
        metadata=redacted_metadata,
        details=redacted_details,
        success=success
    )
    module.add_audit_event(event)

def get_audit_logs(user_id: Optional[str] = None, event_type: Optional[str] = None) -> List[AuditEvent]:
    """Helper function to get audit logs with optional filtering"""
    logs = get_core_module().get_audit_logs()
    if user_id:
        logs = [log for log in logs if log.user_id == user_id]
    if event_type:
        logs = [log for log in logs if log.event_type == event_type]
    return logs

from src.adapters.paypal.transactions import Transaction, TransactionStore, get_transaction_store